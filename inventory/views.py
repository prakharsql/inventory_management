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
from django.http import JsonResponse
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.contrib import messages
from inventory.email import notify_head


# Predefined categories for dropdown
PREDEFINED_CATEGORIES = ["Sensor", "Connector", "Resistor", "Microcontroller"]

# Fields you allow to import and their friendly labels.
# Keys are model field names, values are display labels in mapping UI.

IMPORTABLE_FIELDS = {
    'id': 'Item ID (Auto)',
    'name': 'Item Name (Required)',
    'category': 'Item Category',
    'quantity': 'Initial Stock',
    'reorder_level': 'Minimum Stock Level',
    'unit_price': 'Unit Price (₹)',
    'location': 'Storage Location / Rack',
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

        if not form.is_valid():
            return render(request, 'inventory/import_upload.html', {'form': form})

        f = request.FILES['file']
        filename = f.name.lower()

        if not filename.endswith(ALLOWED_EXTENSIONS):
            messages.error(
                request,
                "Unsupported file type. Upload .xlsx, .xls or .csv"
            )
            return redirect('import_items_upload')

        try:
            # Read file ONCE
            file_bytes = f.read()

            if filename.endswith(('.xls', '.xlsx')):
                df = pd.read_excel(
                    io.BytesIO(file_bytes),
                    engine='openpyxl' if filename.endswith('.xlsx') else None
                )
            else:
                try:
                    df = pd.read_csv(io.BytesIO(file_bytes), encoding='utf-8')
                except Exception:
                    df = pd.read_csv(io.BytesIO(file_bytes), encoding='latin-1')

            preview = df.head(MAX_ROWS_PREVIEW)

        except Exception as e:
            messages.error(request, f"Failed to parse file: {e}")
            return redirect('import_items_upload')

        # Store file safely in session (used by mapping step)
        try:
            import base64
            request.session['import_file_name'] = filename
            request.session['import_file_bytes'] = base64.b64encode(file_bytes).decode('ascii')
            request.session['import_has_header'] = bool(form.cleaned_data.get('has_header', True))
        except Exception:
            messages.warning(
                request,
                "Failed to store file in session — you may need to re-upload on mapping step."
            )
            request.session.pop('import_file_bytes', None)

        cols = list(preview.columns.astype(str))
        preview_rows = preview.fillna('').astype(str).values.tolist()

        context = {
            'cols': cols,
            'preview_rows': preview_rows,
            'importable_fields': IMPORTABLE_FIELDS,
            'filename': filename,
            'has_header': form.cleaned_data.get('has_header', True),
        }

        return render(request, 'inventory/import_mapping.html', context)

    # GET request
    form = ExcelUploadForm()
    return render(request, 'inventory/import_upload.html', {'form': form})


def import_items_map(request):
    """
    Mapping step: user posted mapping selection -> perform import.
    Expects the uploaded file in session as 'import_file_bytes'.
    """
    import base64

    file_b64 = request.session.get('import_file_bytes')
    filename = request.session.get('import_file_name')

    if not file_b64 or not filename:
        messages.error(request, "Upload file first.")
        return redirect('import_items_upload')

    file_bytes = base64.b64decode(file_b64)

    try:
        if filename.endswith(('.xls', '.xlsx')):
            df = pd.read_excel(
                io.BytesIO(file_bytes),
                engine='openpyxl' if filename.endswith('.xlsx') else None
            )
        else:
            try:
                df = pd.read_csv(io.BytesIO(file_bytes), encoding='utf-8')
            except Exception:
                df = pd.read_csv(io.BytesIO(file_bytes), encoding='latin-1')
    except Exception as e:
        messages.error(request, f"Could not read uploaded file: {e}")
        return redirect('import_items_upload')

    cols = list(df.columns.astype(str))

    if request.method == 'POST':
        mapping = {}
        for i, col in enumerate(cols):
            mapped_to = request.POST.get(f'map_{i}')
            if mapped_to:
                mapping[col] = mapped_to

        if not mapping:
            messages.error(request, "No mapping provided. Map at least one column to import.")
            return redirect('import_items_upload')

        # Prevent duplicate field mapping
        if len(set(mapping.values())) != len(mapping.values()):
            messages.error(request, "Each item field can only be mapped once.")
            return redirect('import_items_upload')

        rows = []
        for _, row in df.iterrows():
            item_kwargs = {}
            for col_name, model_field in mapping.items():
                raw_value = row.get(col_name)
                if pd.isna(raw_value):
                    raw_value = None

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

        if len(rows) > MAX_IMPORT_ROWS:
            messages.error(request, f"File too large. Max {MAX_IMPORT_ROWS} rows allowed.")
            return redirect('import_items_upload')

        created = 0
        errors = []

        with transaction.atomic():
            for idx, kw in enumerate(rows, start=1):
                item_data = {k: v for k, v in kw.items() if k in IMPORTABLE_FIELDS}

                if 'category' in item_data and not item_data['category']:
                    item_data['category'] = 'Other'

                item_data.setdefault('quantity', 0)
                item_data.setdefault('reorder_level', 0)
                item_data.setdefault('unit_price', 0.0)

                item_data['is_imported'] = True

                try:
                    Item.objects.create(**item_data)
                    created += 1
                except Exception as e:
                    errors.append(f"Row {idx}: {e}")

        request.session.pop('import_file_bytes', None)
        request.session.pop('import_file_name', None)

        messages.success(request, f"Imported {created} rows.")
        if errors:
            messages.warning(
                request,
                f"Import completed with errors: {len(errors)}. First error: {errors[0]}"
            )

        return redirect('inventory_list')

    preview = df.head(MAX_ROWS_PREVIEW).fillna('').astype(str).values.tolist()

    return render(request, 'inventory/import_mapping.html', {
        'cols': cols,
        'preview_rows': preview,
        'importable_fields': IMPORTABLE_FIELDS,
        'filename': filename,
    })


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
        # ✅ keep user on same page
        page = request.GET.get("page", 1)
        return redirect(f"/inventory/?page={page}")

    return render(request, 'inventory/edit_item.html', {
        'item': item,
        'CATEGORIES': get_all_categories(),
    })


def delete_item(request, item_id):
    if request.method == "POST" and request.headers.get("x-requested-with") == "XMLHttpRequest":
        item = get_object_or_404(Item, id=item_id)
        item.delete()
        return JsonResponse({"success": True})

    return JsonResponse({"success": False}, status=400)


# def add_stock(request, item_id):
#     item = get_object_or_404(Item, id=item_id)
#     if request.method == "POST":
#         qty = int(request.POST.get("quantity"))
#         item.quantity += qty
#         item.save()
#         txn = Transaction.objects.create(item=item, transaction_type='IN', quantity=qty)
#         txn.date = timezone.now()
#         txn.save()
#         messages.success(request, f"{qty} units added to {item.name}")
#         return redirect('inventory_list')
#     return render(request, 'inventory/add_stock.html', {'item': item})


def add_stock(request, item_id):
    item = get_object_or_404(Item, id=item_id)

    if request.method == "POST":
        try:
            qty = int(request.POST.get("quantity"))
            if qty <= 0:
                raise ValueError
        except (TypeError, ValueError):
            messages.error(request, "Please enter a valid positive quantity.")
            return redirect('add_stock', item_id=item.id)

        # Atomic quantity update
        Item.objects.filter(id=item.id).update(
            quantity=F('quantity') + qty
        )

        Transaction.objects.create(
            item=item,
            transaction_type='IN',
            quantity=qty
        )

        messages.success(request, f"{qty} units added to {item.name}")
        # ✅ keep user on same page
        page = request.GET.get("page", 1)
        return redirect(f"/inventory/?page={page}")

    return render(request, 'inventory/add_stock.html', {'item': item})


def remove_stock(request, item_id):
    item = get_object_or_404(Item, id=item_id)

    if request.method == "POST":
        try:
            qty = int(request.POST.get("quantity"))
            if qty <= 0:
                raise ValueError
        except (TypeError, ValueError):
            messages.error(request, "Please enter a valid positive quantity.")
            return redirect('remove_stock', item_id=item.id)

        # Re-fetch current quantity safely
        item.refresh_from_db()

        if qty > item.quantity:
            messages.error(request, "Not enough stock available.")
            return redirect('inventory_list')

        # Atomic quantity update
        Item.objects.filter(id=item.id).update(
            quantity=F('quantity') - qty
        )

        Transaction.objects.create(
            item=item,
            transaction_type='OUT',
            quantity=qty
        )

        messages.success(request, f"{qty} units removed from {item.name}")
        # ✅ keep user on same page
        page = request.GET.get("page", 1)
        return redirect(f"/inventory/?page={page}")

    return render(request, 'inventory/remove_stock.html', {'item': item})



# def transaction_history(request):
#     transactions_qs = (
#         Transaction.objects
#         .select_related('item')
#         .order_by('-date')
#     )

#     paginator = Paginator(transactions_qs, 10)
#     page_number = request.GET.get('page')
#     page_obj = paginator.get_page(page_number)

#     context = {
#         'page_obj': page_obj,
#         'CATEGORIES': get_all_categories(),
#     }

#     return render(request, 'inventory/transaction_history.html', context)
def transaction_history(request):
    search = request.GET.get("search", "").strip()
    category = request.GET.get("category", "").strip()

    transactions = Transaction.objects.select_related("item").order_by("-date")

    # 🔍 GLOBAL SEARCH (across fields)
    if search:
        transactions = transactions.filter(
            Q(item__name__icontains=search) |
            Q(item__category__icontains=search) |
            Q(transaction_type__icontains=search)
        )

    # 🏷 CATEGORY FILTER
    if category:
        transactions = transactions.filter(item__category=category)

    paginator = Paginator(transactions, 10)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    context = {
        "page_obj": page_obj,
        "search": search,
        "category": category,
        "CATEGORIES": get_all_categories(),
    }

    return render(request, "inventory/transaction_history.html", context)

from django.db.models import Q
from django.template.loader import render_to_string
from django.http import JsonResponse

def transaction_live_search(request):
    query = request.GET.get("q", "").strip()
    category = request.GET.get("category", "").strip()

    transactions = Transaction.objects.select_related("item").order_by("-date")

    # 🔍 GLOBAL SEARCH
    if query:
        transactions = transactions.filter(
            Q(item__name__icontains=query) |
            Q(item__category__icontains=query) |
            Q(transaction_type__icontains=query)
        )

    # 🏷 CATEGORY FILTER
    if category:
        transactions = transactions.filter(item__category=category)

    html = render_to_string(
        "inventory/partials/transaction_rows.html",
        {"page_obj": transactions[:50]},  # limit for live search
        request=request
    )

    return JsonResponse({"html": html})



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

    items = Item.objects.filter(
        name__icontains=q,
        quantity__gt=0   # ✅ only available items
    )[:10]

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


@transaction.atomic
def issue_item(request):
    if request.method != 'POST':
        return redirect('issuance_list')

    item_id = request.POST.get("item_id")
    quantity = int(request.POST.get("quantity"))

    item = Item.objects.select_for_update().get(id=item_id)

    # ❌ Block issuing if no stock
    if item.quantity <= 0:
        messages.error(request, "No available item to issue.")
        return redirect("issuance_list")

    # ❌ Block if requested quantity exceeds stock
    if quantity > item.quantity:
        messages.error(
            request,
            f"Only {item.quantity} unit(s) available for {item.name}."
        )
        return redirect("issuance_list")

    issuance =Issuance.objects.create(
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

     # 🔔 EMAIL (THIS MUST EXECUTE)
       # 🔔 EMAIL (THIS MUST EXECUTE)
    notify_head(
        subject="📤 Component Issued",
        message=f"""
<!DOCTYPE html>
<html>
<body style="font-family: Arial, sans-serif; background-color:#f4f6f8; padding:20px;">

<div style="max-width:600px; background:#ffffff; padding:20px; border:1px solid #ddd;">
    
    <h2 style="color:#2e7d32; margin-bottom:10px;">
        📦 Component Issued Successfully
    </h2>

    <p style="font-size:14px;">
        The following component has been <strong>successfully issued</strong>.
    </p>

    <table width="100%" cellpadding="8" cellspacing="0" style="border-collapse:collapse; font-size:14px;">
        <tr>
            <td><strong>Item</strong></td>
            <td>{item.name}</td>
        </tr>
        <tr style="background:#f9f9f9;">
            <td><strong>Quantity</strong></td>
            <td>{quantity}</td>
        </tr>
        <tr>
            <td><strong>Issued By</strong></td>
            <td>{issuance.issuer}</td>
        </tr>
        <tr style="background:#f9f9f9;">
            <td><strong>Receiver</strong></td>
            <td>{issuance.receiver}</td>
        </tr>
        <tr>
            <td><strong>User</strong></td>
            <td>{issuance.user}</td>
        </tr>
        <tr style="background:#f9f9f9;">
            <td><strong>Condition</strong></td>
            <td>{issuance.issue_condition.capitalize()}</td>
        </tr>
    </table>

    <p style="margin-top:20px; font-size:13px; color:#555;">
        🔔 Inventory Management System
    </p>

</div>

</body>
</html>
"""
    )

    messages.success(request, "Component issued successfully.")
    return redirect("issuance_list")


# =====================================================
# RECEIVE ITEM (RETURN FLOW)
# =====================================================
@transaction.atomic
def receive_item(request):

    # issuance = get_object_or_404(Issuance, pk=form.cleaned_data['issuance_id'])

    # # 🚫 HARD STOP for non-returnable
    # if issuance.issue_condition != "returnable":
    #     messages.error(request, "This item is non-returnable and cannot be received.")
    #     return redirect("issuance_list")

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

    # 📧 EMAIL TO HEAD
        # 📧 EMAIL TO HEAD
    notify_head(
        subject="📥 Component Received",
        message=f"""
<!DOCTYPE html>
<html>
<body style="font-family: Arial, sans-serif; background-color:#f4f6f8; padding:20px;">

<div style="max-width:600px; background:#ffffff; padding:20px; border:1px solid #ddd;">

    <h2 style="color:#1565c0; margin-bottom:10px;">
        📥 Component Received
    </h2>

    <table width="100%" cellpadding="8" cellspacing="0" style="border-collapse:collapse; font-size:14px;">
        <tr>
            <td><strong>Item</strong></td>
            <td>{issuance.item.name}</td>
        </tr>
        <tr style="background:#f9f9f9;">
            <td><strong>Quantity</strong></td>
            <td>{issuance.quantity}</td>
        </tr>
        <tr>
            <td><strong>Issued By</strong></td>
            <td>{issuance.issuer}</td>
        </tr>
        <tr style="background:#f9f9f9;">
            <td><strong>Receiver</strong></td>
            <td>{issuance.receiver}</td>
        </tr>
        <tr>
            <td><strong>Status</strong></td>
            <td>{component_status.capitalize()}</td>
        </tr>
        <tr style="background:#f9f9f9;">
            <td><strong>Remark</strong></td>
            <td>{remark or "None"}</td>
        </tr>
    </table>

    <p style="margin-top:20px; font-size:13px; color:#555;">
        Time: {timezone.now().strftime('%d %b %Y, %H:%M')}
        <br>
        🔔 Inventory Management System
    </p>

</div>

</body>
</html>
"""
    )


    messages.success(request, "Component received successfully.")
    return redirect("issuance_list")
# =====================================================

# bulk delete imported items
@require_POST
def delete_imported_items(request):
    """
    Delete all items that were imported via Excel.
    """
    with transaction.atomic():
        imported_items = Item.objects.filter(is_imported=True)
        count = imported_items.count()
        imported_items.delete()

    messages.success(
        request,
        f"{count} imported items deleted successfully."
    )

    return redirect("inventory_list")