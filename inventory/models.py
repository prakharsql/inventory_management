from django.db import models
from django.db.models.signals import post_delete
from django.dispatch import receiver
from django.db import models
from django.utils import timezone
from django.db.models import F
from .constants import ISSUER_CHOICES, COMPONENT_STATUS, ISSUE_CONDITION, CATEGORY_CHOICES


class Item(models.Model):
    
    serial_no = models.PositiveIntegerField(unique=True, blank=True, null=True)
    name = models.CharField(max_length=200)
    category = models.CharField(max_length=100)
    # custom_category = models.CharField(max_length=100, blank=True, null=True)
    quantity = models.PositiveIntegerField(default=0)
    reorder_level = models.PositiveIntegerField()
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    # supplier = models.CharField(max_length=100, blank=True, null=True)
    location = models.CharField(max_length=100, blank=True, null=True)
    # description = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    storage_location = models.CharField(max_length=10, blank=True, null=True, editable=False)#Transaction
    
    is_imported = models.BooleanField(default=False) # To track if the item was imported via CSV

    def save(self, *args, **kwargs):     
        # ✅ Handle custom category
        if self.category == 'Other' and self.custom_category:
            self.category = self.custom_category

        # ✅ Serial number logic
        if not self.serial_no:
            last_item = Item.objects.order_by('-serial_no').first()
            self.serial_no = (last_item.serial_no + 1) if last_item and last_item.serial_no else 1

        # ✅ Assign or update box (storage_location)
        if self.name:
            first_letter = self.name[0].lower()
            new_box = self.assign_box(first_letter)

            # Only update box if name changed or item is new
            if self.pk:
                old_item = Item.objects.filter(pk=self.pk).first()
                if old_item and old_item.name != self.name:
                    self.storage_location = new_box
            else:
                self.storage_location = new_box

        super().save(*args, **kwargs)

    def assign_box(self, letter):
        if letter in ['a', 'b', 'c', 'd', 'e', 'f']:
            return "A1"
        elif letter in ['g', 'h', 'i', 'j', 'k']:
            return "B2"
        elif letter in ['l', 'm', 'n', 'o', 'p']:
            return "C3"
        elif letter in ['q', 'r', 's', 't', 'u']:
            return "D4"
        elif letter in ['v', 'w', 'x', 'y', 'z']:
            return "E5"
        else:
            return None  # fallback for non-alphabetic names


    def stock_status(self):
        if self.quantity == 0:
            return "Out of Stock"
        elif self.quantity <= self.reorder_level:
            return "Low Stock"
        return "In Stock"

    # def __str__(self):
    #     return self.name

    def __str__(self):
        return f"{self.name} ({self.storage_location or 'No Box'})"


class Transaction(models.Model):
    TRANSACTION_TYPES = [
        ('IN', 'Stock In'),
        ('OUT', 'Stock Out'),
    ]
    item = models.ForeignKey(Item, on_delete=models.CASCADE)
    transaction_type = models.CharField(max_length=3, choices=TRANSACTION_TYPES)
    quantity = models.PositiveIntegerField()
    date = models.DateTimeField(auto_now_add=True)
    remarks = models.TextField(blank=True, null=True)

    @property
    def storage_location(self):
        """
        Returns the box name (storage_location) of the associated item.
        Example: "A1", "B2", etc.
        """
        return self.item.storage_location if self.item else None

    def __str__(self):
        box = self.storage_location or "No Box"
        return f"{self.transaction_type} - {self.item.name} ({box})"
    # def __str__(self):
    #     return f"{self.transaction_type} - {self.item.name}"


# ✅ Signal to reorder serial numbers automatically after an item is deleted
@receiver(post_delete, sender=Item)
def reorder_serial_numbers(sender, instance, **kwargs):
    """
    After an item is deleted, this will reorder serial numbers sequentially.
    """
    items = Item.objects.order_by('serial_no')
    for index, item in enumerate(items, start=1):
        if item.serial_no != index:
            item.serial_no = index
            item.save(update_fields=['serial_no'])


class Issuance(models.Model):
    
    item = models.ForeignKey('Item', on_delete=models.CASCADE, related_name='issuances')
    quantity = models.PositiveIntegerField()
    issue_date = models.DateTimeField(auto_now_add=True)
    user = models.CharField(max_length=150)          # person who uses the component
    receiver = models.CharField(max_length=150)      # must be the other issuer (Harsh/Gaurav)
    receive_date = models.DateTimeField(null=True, blank=True)
    component_status = models.CharField(max_length=20, choices=COMPONENT_STATUS, default='ok')
    issuer = models.CharField(max_length=20, choices=ISSUER_CHOICES)
    issue_condition = models.CharField(max_length=20, choices=ISSUE_CONDITION, default='returnable')
    remark = models.TextField(blank=True)
    received = models.BooleanField(default=False)    # whether the item has been returned/received

    class Meta:
        ordering = ['-issue_date']

    def mark_received(self, status: str, remark: str = ''):
        """
        Mark as received: set status, receive_date, increment item quantity if appropriate.
        """
        if self.received:
            return  # already received

        self.component_status = status
        self.remark = (self.remark + "\n" + remark).strip() if remark else self.remark
        self.receive_date = timezone.now()
        self.received = True
        self.save(update_fields=['component_status', 'remark', 'receive_date', 'received'])

        # Add back to stock for OK or Faulty (per requirement)
        if status in ('ok', 'faulty'):
            Item = self.item.__class__
            Item.objects.filter(pk=self.item.pk).update(quantity=F('quantity') + self.quantity)