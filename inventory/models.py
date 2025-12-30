from django.db import models, transaction
from django.db.models import Max, F
from django.utils import timezone
from django.db.models.signals import post_delete
from django.dispatch import receiver


class Item(models.Model):
    """
    Inventory Item
    """

    serial_no = models.PositiveIntegerField(
        unique=True,
        null=True,          # kept nullable ONLY for existing rows
        editable=False
    )

    name = models.CharField(max_length=200)
    category = models.CharField(max_length=100)

    quantity = models.PositiveIntegerField(default=0)
    reorder_level = models.PositiveIntegerField()
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)

    location = models.CharField(max_length=100, blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)

    is_imported = models.BooleanField(default=False)

    def save(self, *args, **kwargs):
        """
        Auto-generate a unique, sequential serial number.
        Concurrency-safe (Excel import, multi-user safe).
        """
        if self.serial_no is None:
            with transaction.atomic():
                last_serial = (
                    Item.objects
                    .select_for_update()
                    .aggregate(max_serial=Max('serial_no'))
                    ['max_serial']
                )
                self.serial_no = (last_serial or 0) + 1

        super().save(*args, **kwargs)

    def stock_status(self):
        if self.quantity == 0:
            return "Out of Stock"
        elif self.quantity <= self.reorder_level:
            return "Low Stock"
        return "In Stock"

    def __str__(self):
        return f"{self.serial_no} - {self.name}"


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

    def __str__(self):
        return f"{self.transaction_type} - {self.item.name}"


class Issuance(models.Model):

    COMPONENT_STATUS = [
        ('ok', 'OK'),
        ('faulty', 'Faulty'),
        ('lost', 'Lost'),
    ]

    ISSUE_CONDITION = [
        ('returnable', 'Returnable'),
        ('non_returnable', 'Non-Returnable'),
    ]

    ISSUER_CHOICES = [
        ('Harsh', 'Harsh'),
        ('Gaurav', 'Gaurav'),
    ]

    item = models.ForeignKey(Item, on_delete=models.CASCADE, related_name='issuances')
    quantity = models.PositiveIntegerField()

    issue_date = models.DateTimeField(auto_now_add=True)
    receive_date = models.DateTimeField(null=True, blank=True)

    user = models.CharField(max_length=150)
    receiver = models.CharField(max_length=150)
    issuer = models.CharField(max_length=20, choices=ISSUER_CHOICES)

    component_status = models.CharField(
        max_length=20,
        choices=COMPONENT_STATUS,
        default='ok'
    )

    issue_condition = models.CharField(
        max_length=20,
        choices=ISSUE_CONDITION,
        default='returnable'
    )

    remark = models.TextField(blank=True)
    received = models.BooleanField(default=False)

    class Meta:
        ordering = ['-issue_date']

    def mark_received(self, status: str, remark: str = ''):
        """
        Mark item as received and update stock if applicable.
        """
        if self.received:
            return

        self.component_status = status
        self.remark = (self.remark + "\n" + remark).strip() if remark else self.remark
        self.receive_date = timezone.now()
        self.received = True
        self.save(update_fields=['component_status', 'remark', 'receive_date', 'received'])

        if status in ('ok', 'faulty'):
            Item.objects.filter(pk=self.item.pk).update(
                quantity=F('quantity') + self.quantity
            )
