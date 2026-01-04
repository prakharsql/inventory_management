from django.urls import path
from . import views

urlpatterns = [
    # Dashboard (Home page)
    path('', views.dashboard, name='dashboard'),

    # Inventory
    path('inventory/', views.inventory_list, name='inventory_list'),
    path('add/', views.add_item, name='add_item'),
    path('edit/<int:item_id>/', views.edit_item, name='edit_item'),
    path('delete/<int:item_id>/', views.delete_item, name='delete_item'),
    path("inventory/live-search/", views.inventory_live_search, name="inventory_live_search"),


    # Stock management
    path('add_stock/<int:item_id>/', views.add_stock, name='add_stock'),
    path('remove_stock/<int:item_id>/', views.remove_stock, name='remove_stock'),

    # Transactions
    path('transactions/', views.transaction_history, name='transaction_history'),
    path("transactions/live-search/", views.transaction_live_search, name="transaction_live_search"),



    # inssuances
    # path('issuances/', views.issuance_list, name='issuance_list'),
    # # path('issuances/issue/', views.issue_item, name='issue_item'),
    # path('issuances/receive/', views.receive_item, name='receive_item'),
    # path("issuances/new/", views.issuance_create, name="issuance_create"),
    # path("autocomplete/items/", views.item_autocomplete, name="item_autocomplete"),

    path("issuances/", views.issuance_list, name="issuance_list"),
    path("issuances/issue/", views.issue_item, name="issue_item"),
    path("issuances/receive/", views.receive_item, name="receive_item"),
    path("items/autocomplete/", views.item_autocomplete, name="item_autocomplete"),


    # Bulk delete imported items
    path("delete-imported/", views.delete_imported_items, name="delete_imported_items"),

     # ... your existing urls ...
    path('import-items/', views.import_items_upload, name='import_items_upload'),
    path('import-items/mapping/', views.import_items_map, name='import_items_map'),
]


