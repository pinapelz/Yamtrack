import json
import logging
import secrets
from functools import wraps

from django.conf import settings
from django.contrib.auth.decorators import login_not_required
from django.db.models import Count, F, Q
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods

from app import config
from app.models import Item, MediaTypes
from app.providers import services
from lists.models import CustomList, CustomListItem
from users.models import VALID_SEARCH_TYPES

logger = logging.getLogger(__name__)


def _extract_api_key(request):
    """Extract the API key from Authorization or X-API-Key headers."""
    api_key = request.headers.get("X-API-Key", "").strip()
    if api_key:
        return api_key

    auth_header = request.headers.get("Authorization", "").strip()
    if auth_header:
        parts = auth_header.split(None, 1)
        if len(parts) == 2 and parts[0].lower() in {"bearer", "token", "api-key"}:  # noqa: PLR2004
            return parts[1].strip()

    return ""


def api_auth(view_func):
    """Authenticate REST API requests using the configured API_KEY setting."""

    @login_not_required
    @csrf_exempt
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        configured_key = (getattr(settings, "API_KEY", None) or "").strip()
        if not configured_key:
            return JsonResponse(
                {"error": "REST API is not configured (API_KEY is not set)."},
                status=503,
            )

        api_key = _extract_api_key(request)
        if not api_key or not secrets.compare_digest(api_key, configured_key):
            logger.warning("REST API request with invalid or missing API key")
            return JsonResponse(
                {"error": "Invalid or missing API key."},
                status=401,
            )

        return view_func(request, *args, **kwargs)

    return _wrapped


def _serialize_item(item, date_added=None):
    """Serialize an Item (and optional list date_added) to a dictionary."""
    return {
        "id": item.id,
        "media_id": item.media_id,
        "source": item.source,
        "media_type": item.media_type,
        "title": item.title,
        "image": item.image,
        "season_number": item.season_number,
        "episode_number": item.episode_number,
        "date_added": date_added.isoformat() if date_added else None,
    }


def _serialize_list_summary(custom_list, items_count=None):
    """Serialize CustomList metadata to a dictionary."""
    return {
        "id": custom_list.id,
        "name": custom_list.name,
        "description": custom_list.description,
        "owner": custom_list.owner.username,
        "collaborators": [u.username for u in custom_list.collaborators.all()],
        "items_count": (
            items_count if items_count is not None else custom_list.items.count()
        ),
    }


def _get_list(list_id):
    """Return the CustomList by ID, or None."""
    try:
        return (
            CustomList.objects.select_related("owner")
            .prefetch_related("collaborators")
            .get(id=list_id)
        )
    except CustomList.DoesNotExist:
        return None


def _parse_request_data(request):
    """Parse JSON body or form data from a request."""
    content_type = request.content_type or ""
    if content_type.startswith("application/json") or (
        request.body and not request.POST
    ):
        try:
            data = json.loads(request.body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            msg = "Invalid JSON payload."
            raise ValueError(msg) from exc
        if not isinstance(data, dict):
            msg = "JSON payload must be an object."
            raise ValueError(msg)
        return data
    return request.POST.dict()


def _parse_optional_positive_int(value, field_name):
    """Parse an optional positive integer field or raise ValueError."""
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        msg = f"Invalid '{field_name}': must be a positive integer."
        raise ValueError(msg) from exc
    if parsed < 0:
        msg = f"Invalid '{field_name}': must be a non-negative integer."
        raise ValueError(msg)
    return parsed


def _validate_season_episode(media_type, season_number, episode_number):
    """Validate season_number and episode_number constraints for media_type."""
    if media_type == MediaTypes.SEASON.value:
        if season_number is None:
            msg = "'season_number' is required when media_type is 'season'."
            raise ValueError(msg)
        if episode_number is not None:
            msg = "'episode_number' cannot be set when media_type is 'season'."
            raise ValueError(msg)
    elif media_type == MediaTypes.EPISODE.value:
        if season_number is None or episode_number is None:
            msg = (
                "Both 'season_number' and 'episode_number' are required "
                "when media_type is 'episode'."
            )
            raise ValueError(msg)
    elif season_number is not None or episode_number is not None:
        msg = (
            "'season_number' and 'episode_number' are only allowed "
            "for 'season' and 'episode' media types."
        )
        raise ValueError(msg)


def _resolve_source(media_type, source):
    """Resolve and validate the provider source for a media_type."""
    valid_sources = [s.value for s in config.get_sources(media_type)]
    if not source:
        return config.get_default_source_name(media_type).value
    if source not in valid_sources:
        msg = (
            f"Invalid source '{source}' for media_type '{media_type}'. "
            f"Valid sources: {', '.join(valid_sources)}."
        )
        raise ValueError(msg)
    return source


def _resolve_item_by_media_id(data):
    """Find or create an Item from media_id and media_type."""
    media_id = str(data["media_id"]).strip()
    if not media_id:
        msg = "'media_id' cannot be empty."
        raise ValueError(msg)

    media_type = data.get("media_type")
    if not media_type or media_type not in MediaTypes.values:
        valid = ", ".join(MediaTypes.values)
        msg = f"Valid 'media_type' is required with 'media_id'. Valid types: {valid}."
        raise ValueError(msg)

    season_number = _parse_optional_positive_int(
        data.get("season_number"),
        "season_number",
    )
    episode_number = _parse_optional_positive_int(
        data.get("episode_number"),
        "episode_number",
    )
    _validate_season_episode(media_type, season_number, episode_number)
    source = _resolve_source(media_type, data.get("source"))

    try:
        return Item.objects.get(
            media_id=media_id,
            source=source,
            media_type=media_type,
            season_number=season_number,
            episode_number=episode_number,
        )
    except Item.DoesNotExist:
        metadata = services.get_media_metadata(
            media_type,
            media_id,
            source,
            [season_number],
            episode_number,
        )
        item, _ = Item.objects.get_or_create(
            media_id=media_id,
            source=source,
            media_type=media_type,
            season_number=season_number,
            episode_number=episode_number,
            defaults={
                "title": metadata["title"],
                "image": metadata.get("image") or settings.IMG_NONE,
            },
        )
        return item


def _resolve_item_by_query(data):
    """Search for media by query and return or create the top matching Item."""
    query = str(data.get("query") or data.get("q") or "").strip()
    if not query:
        msg = "Search 'query' cannot be empty."
        raise ValueError(msg)

    media_type = data.get("media_type") or MediaTypes.MOVIE.value
    if media_type not in VALID_SEARCH_TYPES:
        valid = ", ".join(VALID_SEARCH_TYPES)
        msg = f"Invalid 'media_type' '{media_type}'. Valid search types: {valid}."
        raise ValueError(msg)

    source = _resolve_source(media_type, data.get("source"))
    search_data = services.search(
        media_type,
        query,
        1,
        source,
    )
    results = search_data.get("results") or []
    if not results:
        return None

    top_match = results[0]
    item, _ = Item.objects.get_or_create(
        media_id=str(top_match["media_id"]),
        source=top_match["source"],
        media_type=top_match["media_type"],
        season_number=None,
        episode_number=None,
        defaults={
            "title": top_match["title"],
            "image": top_match.get("image") or settings.IMG_NONE,
        },
    )
    return item


@api_auth
@require_GET
def lists_collection(request):
    """Return custom lists, optionally filtered by query or owner."""
    search_query = request.GET.get("q", "").strip()
    owner_username = request.GET.get("owner", "").strip()
    custom_lists = (
        CustomList.objects.select_related("owner")
        .prefetch_related("collaborators")
        .annotate(items_count=Count("items", distinct=True))
        .distinct()
        .order_by("name")
    )

    if owner_username:
        custom_lists = custom_lists.filter(
            Q(owner__username__iexact=owner_username)
            | Q(collaborators__username__iexact=owner_username),
        )

    if search_query:
        custom_lists = custom_lists.filter(
            Q(name__icontains=search_query) | Q(description__icontains=search_query),
        )

    return JsonResponse(
        {
            "lists": [
                _serialize_list_summary(cl, cl.items_count) for cl in custom_lists
            ],
        },
    )


def _get_list_items_response(request, custom_list):
    """Build the JSON response for a custom list and its items."""
    list_items = CustomListItem.objects.filter(
        custom_list=custom_list,
    ).select_related("item")

    search_query = request.GET.get("q", "").strip()
    if search_query:
        list_items = list_items.filter(item__title__icontains=search_query)

    media_type = request.GET.get("media_type") or request.GET.get("type")
    if media_type and media_type != "all":
        list_items = list_items.filter(item__media_type=media_type)

    sort_by = request.GET.get("sort", "date_added")
    sort_mapping = {
        "date_added": ["-date_added"],
        "title": [
            F("item__title").asc(nulls_last=True),
            F("item__season_number").asc(nulls_first=True),
            F("item__episode_number").asc(nulls_first=True),
        ],
        "media_type": ["item__media_type", "-date_added"],
    }
    list_items = list_items.order_by(
        *sort_mapping.get(sort_by, ["-date_added"]),
    )

    serialized_items = [
        _serialize_item(cli.item, cli.date_added) for cli in list_items
    ]
    return JsonResponse(
        {
            **_serialize_list_summary(custom_list, len(serialized_items)),
            "items": serialized_items,
        },
    )


def _resolve_item_from_payload(data):
    """Resolve an Item from payload (by item_id, media_id, or query) or return error."""
    if data.get("item_id") not in (None, ""):
        item_id = _parse_optional_positive_int(data.get("item_id"), "item_id")
        try:
            return Item.objects.get(id=item_id), None
        except Item.DoesNotExist:
            return None, JsonResponse({"error": "Item not found."}, status=404)

    if data.get("media_id") not in (None, ""):
        return _resolve_item_by_media_id(data), None

    if data.get("query") or data.get("q"):
        item = _resolve_item_by_query(data)
        if item is None:
            return None, JsonResponse(
                {"error": "No media found matching query."},
                status=404,
            )
        return item, None

    return None, JsonResponse(
        {
            "error": (
                "Provide 'item_id', 'media_id' (with 'media_type'), "
                "or 'query' (with optional 'media_type')."
            ),
        },
        status=400,
    )


def _add_item_to_list_response(request, custom_list):
    """Handle adding an item to a custom list via ID, provider ID, or query."""
    try:
        data = _parse_request_data(request)
        item, error_response = _resolve_item_from_payload(data)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    except services.ProviderAPIError as exc:
        status_code = 404 if exc.status_code == 404 else 502  # noqa: PLR2004
        return JsonResponse({"error": str(exc)}, status=status_code)

    if error_response is not None:
        return error_response

    list_item, created = CustomListItem.objects.get_or_create(
        custom_list=custom_list,
        item=item,
    )
    logger.info(
        "API: %s %s %s",
        item,
        "added to" if created else "already in",
        custom_list,
    )
    return JsonResponse(
        {
            "added": created,
            "list_id": custom_list.id,
            "item": _serialize_item(item, list_item.date_added),
        },
        status=201 if created else 200,
    )


@api_auth
@require_http_methods(["GET", "POST"])
def list_items(request, list_id):
    """Get items in a custom list or add an item to the custom list."""
    custom_list = _get_list(list_id)
    if custom_list is None:
        return JsonResponse({"error": "List not found."}, status=404)

    if request.method == "GET":
        return _get_list_items_response(request, custom_list)

    return _add_item_to_list_response(request, custom_list)


@api_auth
@require_http_methods(["DELETE"])
def list_item_detail(_request, list_id, item_id):
    """Remove an item from a custom list."""
    custom_list = _get_list(list_id)
    if custom_list is None:
        return JsonResponse({"error": "List not found."}, status=404)

    deleted_count, _ = CustomListItem.objects.filter(
        custom_list=custom_list,
        item_id=item_id,
    ).delete()
    if not deleted_count:
        return JsonResponse({"error": "Item not found in list."}, status=404)

    logger.info(
        "API: item %s removed from %s",
        item_id,
        custom_list,
    )
    return JsonResponse(
        {
            "removed": True,
            "list_id": custom_list.id,
            "item_id": item_id,
        },
    )


@api_auth
@require_GET
def search_media(request):
    """Search external media providers for items that can be added to a list."""
    query = (request.GET.get("q") or request.GET.get("query") or "").strip()
    if not query:
        return JsonResponse(
            {"error": "Missing required search query parameter 'q'."},
            status=400,
        )

    media_type = request.GET.get("media_type") or MediaTypes.MOVIE.value
    if media_type not in VALID_SEARCH_TYPES:
        valid = ", ".join(VALID_SEARCH_TYPES)
        return JsonResponse(
            {
                "error": (
                    f"Invalid 'media_type' '{media_type}'. Valid search types: {valid}."
                ),
            },
            status=400,
        )

    try:
        source = _resolve_source(media_type, request.GET.get("source"))
        page = _parse_optional_positive_int(request.GET.get("page"), "page") or 1
        if page < 1:
            return JsonResponse(
                {"error": "Invalid 'page': must be >= 1."},
                status=400,
            )
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)

    try:
        data = services.search(
            media_type,
            query,
            page,
            source,
        )
    except services.ProviderAPIError as exc:
        status_code = 404 if exc.status_code == 404 else 502  # noqa: PLR2004
        return JsonResponse({"error": str(exc)}, status=status_code)

    return JsonResponse(
        {
            "source": source,
            "media_type": media_type,
            **data,
        },
    )
