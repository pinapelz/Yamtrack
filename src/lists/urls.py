from django.urls import path

from lists import api, views

urlpatterns = [
    path("lists", views.lists, name="lists"),
    path(
        "lists_modal/<source:source>/<media_type:media_type>/<str:media_id>",
        views.lists_modal,
        name="lists_modal",
    ),
    path(
        "lists_modal/<source:source>/<media_type:media_type>/<str:media_id>/<int:season_number>",
        views.lists_modal,
        name="lists_modal",
    ),
    path(
        "lists_modal/<source:source>/<media_type:media_type>/<str:media_id>/<int:season_number>/<int:episode_number>",
        views.lists_modal,
        name="lists_modal",
    ),
    path("list/<int:list_id>", views.list_detail, name="list_detail"),
    path("list/create", views.create, name="list_create"),
    path("list/edit", views.edit, name="list_edit"),
    path("list/delete", views.delete, name="list_delete"),
    path("list_item_toggle", views.list_item_toggle, name="list_item_toggle"),
    path("api/lists", api.lists_collection, name="api_lists"),
    path("api/lists/", api.lists_collection),
    path("api/lists/<int:list_id>", api.list_items, name="api_list_detail"),
    path("api/lists/<int:list_id>/", api.list_items),
    path("api/lists/<int:list_id>/items", api.list_items, name="api_list_items"),
    path("api/lists/<int:list_id>/items/", api.list_items),
    path(
        "api/lists/<int:list_id>/items/<int:item_id>",
        api.list_item_detail,
        name="api_list_item_detail",
    ),
    path("api/lists/<int:list_id>/items/<int:item_id>/", api.list_item_detail),
    path("api/search", api.search_media, name="api_search"),
    path("api/search/", api.search_media),
]
