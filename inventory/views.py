from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from .models import Item, Transaction, Issuance
from django.db.models import F
from django.core.paginator import Paginator
from django.utils import timezone
from django.db import transaction
from .forms import IssuanceForm, ReceiveForm
import io
import pandas as pd
from django.urls import reverse
from .forms import ExcelUploadForm, ColumnMappingForm, IssuanceForm, ReceiveForm
from .utils import get_all_categories
from django.template.loader import render_to_string
from django.http import JsonResponse
from django.views.decorators.http import require_POST, require_GET
from django.db.models import Q, CharField
from django.db.models.functions import Cast
from django.db.models import Q
from django.core.paginator import Paginator

# Predefined categories for dropdown
PREDEFINED_CATEGORIES = ["Sensor", "Connector", "Resistor", "Microcontroller"]

# Fields you allow to import and their friendly labels.
# Keys are model field names, values are display labels in mapping UI.
IMPORTABLE_FIELDS = {
    'name': 'Name',
    'category': 'Category',
    'quantity': 'Quantity',
    'reorder_level': 'Reorder Level',
    'unit_price': 'Unit Price',
    'supplier': 'Supplier',
    'location': 'Storage Location',
    'description': 'Description',
}

ALLOWED_EXTENSIONS = ('.xlsx', '.xls', '.csv')
MAX_ROWS_PREVIEW = 5
MAX_IMPORT_ROWS = 5000  # safety cap; adjust as needed

def import_items_upload(request):
    """
    First step: upload file and preview header/first rows.
    """
    if request.method == 'POST':
        form = ExcelUploadForm(request.POST, request.FILES)
        if form.is_valid():
            f = request.FILES['file']
            filename = f.name.lower()

            if not filename.endswith(ALLOWED_EXTENSIONS):
                messages.error(request, "Unsupported file type. Upload .xlsx, .xls or .csv")
                return redirect('import_items_upload')

            # Read into pandas DataFrame
            try:
                # read excel or csv
                if filename.endswith(('.xls', '.xlsx')):
                    df = pd.read_excel(f, engine='openpyxl' if filename.endswith('.xlsx') else None)
                else:
                    # csv
                    file_bytes = f.read()
                    encoding = 'utf-8'
                    try:
                        df = pd.read_csv(io.BytesIO(file_bytes), encoding=encoding)
                    except Exception:
                        # fallback with latin-1
                        df = pd.read_csv(io.BytesIO(file_bytes), encoding='latin-1')
                # Limit rows for preview/safety
                preview = df.head(MAX_ROWS_PREVIEW)
            except Exception as e:
                messages.error(request, f"Failed to parse file: {e}")
                return redirect('import_items_upload')

            # store raw data in session as JSON-friendly format (list of dicts) OR in memory via request.FILES? 
            # We'll store small preview + entire file in session via bytes (base64) is heavy — better: store file in temp
            # For simplicity we'll save uploaded file in request.FILES -> but we need persistence across request.
            # Simpler approach: save file bytes into session (if small). We'll limit to reasonable sizes.
            try:
                f.seek(0)
                data_bytes = f.read()
                # store in session as base64 string
                import base64
                request.session['import_file_name'] = filename
                request.session['import_file_bytes'] = base64.b64encode(data_bytes).decode('ascii')
                request.session['import_has_header'] = bool(form.cleaned_data.get('has_header', True))
            except Exception as e:
                messages.warning(request, "Failed to store file in session — you may need to re-upload on mapping step.")
                # fallback: provide mapping immediately without persisting file
                request.session.pop('import_file_bytes', None)

            # build columns list for mapping UI
            cols = list(preview.columns.astype(str))
            # convert preview to list-of-lists for template
            preview_rows = preview.fillna('').astype(str).values.tolist()

            context = {
                'cols': cols,
                'preview_rows': preview_rows,
                'importable_fields': IMPORTABLE_FIELDS,
                'filename': filename,
                'has_header': form.cleaned_data.get('has_header', True),
            }
            return render(request, 'inventory/import_mapping.html', context)
    else:
        form = ExcelUploadForm()
    return render(request, 'inventory/import_upload.html', {'form': form})

def import_items_map(request):
    """
    Mapping step: user posted mapping selection -> perform import.
    Expects the uploaded file in session as 'import_file_bytes'.
    """
    # Reconstruct DataFrame from session
    import base64
    file_b64 = request.session.get('import_file_bytes')
    filename = request.session.get('import_file_name')
    if not file_b64:
        messages.error(request, "Upload file first.")
        return redirect('import_items_upload')

    file_bytes = base64.b64decode(file_b64)
    try:
        if filename.endswith(('.xls', '.xlsx')):
            df = pd.read_excel(io.BytesIO(file_bytes), engine='openpyxl' if filename.endswith('.xlsx') else None)
        else:
            # csv fallback
            df = pd.read_csv(io.BytesIO(file_bytes), encoding='utf-8')
    except Exception as e:
        messages.error(request, f"Could not read uploaded file: {e}")
        return redirect('import_items_upload')

    cols = list(df.columns.astype(str))
    # Read mapping submitted by user
    if request.method == 'POST':
        # mapping keys are like 'map_0', 'map_1' etc representing column indices
        mapping = {}
        for i, col in enumerate(cols):
            mapped_to = request.POST.get(f'map_{i}')
            if mapped_to:
                mapping[col] = mapped_to  # mapped_to is model field name
        if not mapping:
            messages.error(request, "No mapping provided. Map at least one column to import.")
            return redirect('import_items_upload')

        # Build rows to import
        rows = []
        for _, row in df.iterrows():
            item_kwargs = {}
            for col_name, model_field in mapping.items():
                # get value safely and convert for known numeric fields
                raw_value = row.get(col_name, None)
                if pd.isna(raw_value):
                    raw_value = None
                # convert types if target is numeric
                if model_field in ('quantity', 'reorder_level'):
                    try:
                        item_kwargs[model_field] = int(raw_value) if raw_value is not None else 0
                    except Exception:
                        item_kwargs[model_field] = 0
                elif model_field == 'unit_price':
                    try:
                        item_kwargs[model_field] = float(raw_value) if raw_value is not None else 0.0
                    except Exception:
                        item_kwargs[model_field] = 0.0
                else:
                    item_kwargs[model_field] = str(raw_value).strip() if raw_value is not None else ''
            rows.append(item_kwargs)

        # safety cap
        if len(rows) > MAX_IMPORT_ROWS:
            messages.error(request, f"File too large. Max {MAX_IMPORT_ROWS} rows allowed.")
            return redirect('import_items_upload')

        # Start DB import transaction
        created = 0
        errors = []
        with transaction.atomic():
            for idx, kw in enumerate(rows, start=1):
                # Build final kwargs for Item.create. Only include allowed fields.
                item_data = {k: v for k, v in kw.items() if k in IMPORTABLE_FIELDS}
                # if category field is empty, set default
                if 'category' in item_data and not item_data['category']:
                    item_data['category'] = 'Other'
                # Ensure numeric fields have defaults
                item_data.setdefault('quantity', 0)
                item_data.setdefault('reorder_level', 0)
                item_data.setdefault('unit_price', 0.0)

                item_data['is_imported'] = True # mark as imported

                # Validate (basic)
                try:
                    Item.objects.create(**item_data)
                    created += 1
                except Exception as e:
                    errors.append(f"Row {idx}: {e}")
                    # depending on your policy you can rollback entire transaction or continue; here we continue but still inside transaction
            # commit happens automatically if no exception
        # cleanup session
        request.session.pop('import_file_bytes', None)
        request.session.pop('import_file_name', None)
        messages.success(request, f"Imported {created} rows.")
        if errors:
            messages.warning(request, f"Import completed with errors: {len(errors)}. First error: {errors[0]}")
        return redirect('inventory_list')

    # GET: render mapping UI if user arrives without POST (fallback)
    preview = df.head(MAX_ROWS_PREVIEW).fillna('').astype(str).values.tolist()
    context = {
        'cols': cols,
        'preview_rows': preview,
        'importable_fields': IMPORTABLE_FIELDS,
        'filename': filename,
    }
    return render(request, 'inventory/import_mapping.html', context)



# def dashboard(request):
#     total_items = Item.objects.count()
#     low_stock = Item.objects.filter(quantity__gt=0, quantity__lte=F('reorder_level')).count()
#     out_stock = Item.objects.filter(quantity=0).count()
#     items = Item.objects.all().order_by('serial_no')
#     paginator = Paginator(items, 50)
#     page_number = request.GET.get('page')
#     page_obj = paginator.get_page(page_number)
#     context = {
#         'total_items': total_items,
#         'low_stock': low_stock,
#         'out_stock': out_stock,
#         'items': items,
#         'CATEGORIES': get_all_categories(),
#         'page_obj': page_obj,
#     }
#     return render(request, 'inventory/dashboard.html', context)

def dashboard(request):
    total_items = Item.objects.count()
    low_stock = Item.objects.filter(
        quantity__gt=0,
        quantity__lte=F('reorder_level')
    ).count()
    out_stock = Item.objects.filter(quantity=0).count()

    items_qs = Item.objects.order_by('serial_no')

    paginator = Paginator(items_qs, 50)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    context = {
        'total_items': total_items,
        'low_stock': low_stock,
        'out_stock': out_stock,
        'page_obj': page_obj,
    }

    return render(request, 'inventory/dashboard.html', context)


def inventory_list(request):
    query = request.GET.get("q", "").strip()
    category = request.GET.get("category", "").strip()
    page_number = request.GET.get("page")

    items = Item.objects.all()

    # 🔍 GLOBAL SEARCH (ALL TEXT COLUMNS)
    if query:
        items = items.filter(
            Q(name__icontains=query) |
            Q(category__icontains=query) |
            Q(location__icontains=query)
        )

    # 🏷 CATEGORY FILTER (INDEPENDENT)
    if category:
        items = items.filter(category__iexact=category)

    items = items.order_by("serial_no")

    paginator = Paginator(items, 50)
    page_obj = paginator.get_page(page_number)

    context = {
        "page_obj": page_obj,
        "CATEGORIES": get_all_categories(),
        "query": query,
        "selected_category": category,
    }

    return render(request, "inventory/inventory_list.html", context)


def inventory_live_search(request):
    query = request.GET.get("q", "").strip()
    category = request.GET.get("category", "").strip()

    items = Item.objects.all()

    # 🔍 GLOBAL WORD SEARCH (across fields)
    if query:
        items = items.filter(
            Q(name__icontains=query) |
            Q(category__icontains=query) |
            Q(location__icontains=query)
        )

    # 🏷 CATEGORY FILTER (if selected)
    if category:
        items = items.filter(category__iexact=category)

    items = items.order_by("serial_no")

    html = render_to_string(
        "inventory/partials/inventory_rows.html",
        {"page_obj": items[:50]},  # no pagination for live search
        request=request
    )

    return JsonResponse({"html": html})



def add_item(request):
    # if request.method == "POST":
    #     name = request.POST.get('name')
    #     category = request.POST.get('category')
    #     # custom_category = request.POST.get('custom_category')
    #     quantity = request.POST.get('quantity')
    #     reorder_level = request.POST.get('reorder_level')
    #     unit_price = request.POST.get('unit_price')
    #     # supplier = request.POST.get('supplier')
    #     location = request.POST.get('location')
    #     # description = request.POST.get('description')

    if request.method == "POST":
        name = request.POST.get('name')
        category = request.POST.get('category')
        custom_category = request.POST.get('custom_category')

        try:
            quantity = int(request.POST.get('quantity'))
            reorder_level = int(request.POST.get('reorder_level'))
            unit_price = float(request.POST.get('unit_price'))
        except (TypeError, ValueError):
            messages.error(
                request,
                "Please enter valid numbers for quantity, reorder level, and unit price."
            )
            return redirect('add_item')

        if quantity < 0 or reorder_level < 0 or unit_price < 0:
            messages.error(request, "Negative values are not allowed.")
            return redirect('add_item')

        #FINAL CATEGORY LOGIC
        final_category = (
            custom_category.strip()
            if category == "Other" and custom_category
            else category
        )
        # final_category = custom_category.strip() if category == "Other" and custom_category else category

        Item.objects.create(
            name=name,
            category=final_category,
            quantity=quantity,
            reorder_level=reorder_level,
            unit_price=unit_price,
            location=request.POST.get('location')
        )

        messages.success(request, "Item added successfully!")
        return redirect('inventory_list')
    
    return render(request, 'inventory/add_item.html', {
        'CATEGORIES': get_all_categories(),
    })


def edit_item(request, item_id):
    item = get_object_or_404(Item, id=item_id)
    if request.method == "POST":
        item.name = request.POST.get('name')
        category = request.POST.get('category')
        custom_category = request.POST.get('custom_category')
        item.category = custom_category if category == "Other" and custom_category else category
        try:
            item.quantity = int(request.POST.get('quantity'))
            item.reorder_level = int(request.POST.get('reorder_level'))
            item.unit_price = float(request.POST.get('unit_price'))
        except (TypeError, ValueError):
            messages.error(request, "Please enter valid numeric values.")
            return redirect('edit_item', item_id=item.id)

        item.location = request.POST.get('location')

        item.save()
        messages.success(request, "Item updated successfully!")
        return redirect('inventory_list')

    return render(request, 'inventory/edit_item.html', {
        'item': item,
        'CATEGORIES': get_all_categories(),
    })


def delete_item(request, item_id):
    item = get_object_or_404(Item, id=item_id)
    item.delete()
    messages.success(request, "Item deleted successfully!")
    return redirect('inventory_list')


def add_stock(request, item_id):
    item = get_object_or_404(Item, id=item_id)
    if request.method == "POST":
        qty = int(request.POST.get("quantity"))
        item.quantity += qty
        item.save()
        txn = Transaction.objects.create(item=item, transaction_type='IN', quantity=qty)
        txn.date = timezone.now()
        txn.save()
        messages.success(request, f"{qty} units added to {item.name}")
        return redirect('inventory_list')
    return render(request, 'inventory/add_stock.html', {'item': item})


def remove_stock(request, item_id):
    item = get_object_or_404(Item, id=item_id)
    if request.method == "POST":
        qty = int(request.POST.get("quantity"))
        if qty > item.quantity:
            messages.error(request, "Not enough stock available")
        else:
            item.quantity -= qty
            item.save()
            txn = Transaction.objects.create(item=item, transaction_type='OUT', quantity=qty)
            txn.date = timezone.now()
            txn.save()
            messages.success(request, f"{qty} units removed from {item.name}")
        return redirect('inventory_list')
    return render(request, 'inventory/remove_stock.html', {'item': item})


def transaction_history(request):
    transactions = Transaction.objects.select_related('item').order_by('-date')
    paginator = Paginator(transactions, 10)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)
    context = {
        'transactions': transactions,
        'CATEGORIES': get_all_categories(),
        'page_obj': page_obj,
    }
    return render(request, 'inventory/transaction_history.html', context)


# ----------------------------- #
#     ISSUER PAGE SECTION       #
# ----------------------------- #

# def issuance_list(request):
#     """Display all issuances and provide issue/receive actions."""
#     issuances = Issuance.objects.select_related('item').all().order_by('-issue_date')
#     # form = IssuanceForm()
#     # receive_form = ReceiveForm()
#     return render(request, 'inventory/issuance_list.html', {
#         'issuances': issuances,
#         # 'form': form,
#         # 'receive_form': receive_form,
#     })



# =====================================================
# ISSUANCE LIST PAGE
# =====================================================
def issuance_list(request):
    issuances = Issuance.objects.select_related("item").order_by("-issue_date")
    items = Item.objects.all().order_by("name")  # for dropdown / autocomplete

    return render(request, "inventory/issuance_list.html", {
        "issuances": issuances,
        "items": items,
        "issuance_form": IssuanceForm(),
        "receive_form": ReceiveForm(),
    })


# =====================================================
# ITEM AUTOCOMPLETE (330 Ω behaviour)
# =====================================================
@require_GET
def item_autocomplete(request):
    q = request.GET.get("q", "").strip()

    items = Item.objects.filter(name__icontains=q).order_by("name")[:10]

    return JsonResponse([
        {
            "id": item.id,
            "name": item.name,
            "category": item.category,
            "quantity": item.quantity
        }
        for item in items
    ], safe=False)


# =====================================================
# ISSUE ITEM (CREATE ISSUANCE) – SINGLE SOURCE OF TRUTH
# =====================================================
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.contrib import messages

@transaction.atomic
def issue_item(request):
    if request.method != 'POST':
        return redirect('issuance_list')

    item_id = request.POST.get("item_id")
    quantity = int(request.POST.get("quantity"))

    item = Item.objects.select_for_update().get(id=item_id)

    if item.quantity < quantity:
        messages.error(request, f"Not enough stock. Available: {item.quantity}")
        return redirect("issuance_list")

    Issuance.objects.create(
        item=item,
        quantity=quantity,
        user=request.POST.get("user"),
        receiver=request.POST.get("receiver"),
        issuer=request.POST.get("issuer"),
        issue_condition=request.POST.get("issue_condition"),
        remark=request.POST.get("remark", "")
    )

    item.quantity -= quantity
    item.save()

    messages.success(request, "Component issued successfully.")
    return redirect("issuance_list")


    Issuance.objects.create(
        item=item,
        quantity=quantity,
        user=request.POST.get("user"),
        receiver=request.POST.get("receiver"),
        issuer=request.POST.get("issuer"),
        issue_condition=request.POST.get("issue_condition"),
        remark=request.POST.get("remark", ""),
        component_status="issued",
        issue_date=timezone.now(),
        received=False,
    )

    # 🔻 deduct stock
    item.quantity -= quantity
    item.save(update_fields=["quantity"])

    messages.success(request, "Component issued successfully.")
    return redirect("issuance_list")


# =====================================================
# RECEIVE ITEM (RETURN FLOW)
# =====================================================
@transaction.atomic
def receive_item(request):
    if request.method != "POST":
        return redirect("issuance_list")

    issuance_id = request.POST.get("issuance_id")
    component_status = request.POST.get("component_status")
    remark = request.POST.get("remark", "")

    issuance = Issuance.objects.select_for_update().get(id=issuance_id)

    if issuance.received:
        messages.warning(request, "This item is already received.")
        return redirect("issuance_list")

    issuance.component_status = component_status
    issuance.remark = remark
    issuance.receive_date = timezone.now()
    issuance.received = True
    issuance.save()

    # 🔼 add stock back ONLY if OK or FAULTY
    if component_status in ["ok", "faulty"]:
        Item.objects.filter(id=issuance.item.id).update(
            quantity=F("quantity") + issuance.quantity
        )

    messages.success(request, "Component received successfully.")
    return redirect("issuance_list")
# =====================================================


#bulk delete imported items
def delete_imported_items(request):
    """
    Deletes all items that were imported via Excel.
    """
    if request.method == "POST":
        with transaction.atomic():
            qs = Item.objects.filter(is_imported=True)
            count = qs.count()
            qs.delete()

        messages.success(
            request,
            f"{count} imported items deleted successfully."
        )
    else:
        messages.error(request, "Invalid request.")

    return redirect("inventory_list")