import re
from functools import partial
from typing import ClassVar, cast

import urlman
from asgiref.sync import sync_to_async
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.safestring import mark_safe

from core.files import get_remote_file
from core.html import strip_html
from core.models import Config
from core.uploads import upload_emoji_namer
from core.uris import AutoAbsoluteUrl, RelativeAbsoluteUrl, StaticAbsoluteUrl
from stator.models import State, StateField, StateGraph, StatorModel
from users.models import Domain


class EmojiStates(StateGraph):
    outdated = State(try_interval=300, force_initial=True)
    updated = State()

    outdated.transitions_to(updated)

    @classmethod
    async def handle_outdated(cls, instance: "Emoji"):
        """
        Fetches remote emoji and uploads to file for local caching
        """
        if instance.remote_url and not instance.file:
            file, mimetype = await get_remote_file(
                instance.remote_url,
                timeout=settings.SETUP.REMOTE_TIMEOUT,
                max_size=settings.SETUP.EMOJI_MAX_IMAGE_FILESIZE_KB * 1024,
            )
            if file:
                instance.file = file
                instance.mimetype = mimetype
                await sync_to_async(instance.save)()

        return cls.updated


class EmojiQuerySet(models.QuerySet):
    def usable(self, domain: Domain | None = None):
        if domain is None or domain.local:
            visible_q = models.Q(local=True)
        else:
            visible_q = models.Q(public=True)
            if Config.system.emoji_unreviewed_are_public:
                visible_q |= models.Q(public__isnull=True)

        qs = self.filter(visible_q)
        if domain:
            if not domain.local:
                qs = qs.filter(domain=domain)
        return qs


class EmojiManager(models.Manager):
    def get_queryset(self):
        return EmojiQuerySet(self.model, using=self._db)

    def usable(self, domain: Domain | None = None):
        return self.get_queryset().usable(domain)


class Emoji(StatorModel):

    # Normalized Emoji without the ':'
    shortcode = models.SlugField(max_length=100, db_index=True)

    domain = models.ForeignKey(
        "users.Domain", null=True, blank=True, on_delete=models.CASCADE
    )
    local = models.BooleanField(default=True)

    # Should this be shown in the public UI?
    public = models.BooleanField(null=True)

    object_uri = models.CharField(max_length=500, blank=True, null=True, unique=True)

    mimetype = models.CharField(max_length=200)

    # Files may not be populated if it's remote and not cached on our side yet
    file = models.ImageField(
        upload_to=partial(upload_emoji_namer, "emoji"),
        null=True,
        blank=True,
    )

    # A link to the custom emoji
    remote_url = models.CharField(max_length=500, blank=True, null=True)

    # Used for sorting custom emoji in the picker
    category = models.CharField(max_length=100, blank=True, null=True)

    # State of this Emoji
    state = StateField(EmojiStates)

    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    objects = EmojiManager()

    # Cache of the local emojis {shortcode: Emoji}
    locals: ClassVar["dict[str, Emoji]"]

    class Meta:
        unique_together = ("domain", "shortcode")

    class urls(urlman.Urls):
        root = "/admin/emoji/"
        create = "{root}/create/"
        edit = "{root}{self.Emoji}/"
        delete = "{edit}delete/"

    emoji_regex = re.compile(r"\B:([a-zA-Z0-9(_)-]+):\B")

    def clean(self):
        super().clean()
        if self.local ^ (self.domain is None):
            raise ValidationError("Must be local or have a domain")

    def __str__(self):
        return f"{self.id}-{self.shortcode}"

    @classmethod
    def load_locals(cls) -> dict[str, "Emoji"]:
        return {x.shortcode: x for x in Emoji.objects.usable().filter(local=True)}

    @property
    def fullcode(self):
        return f":{self.shortcode}:"

    @property
    def is_usable(self) -> bool:
        """
        Return True if this Emoji is usable.
        """
        return self.public or (
            self.public is None and Config.system.emoji_unreviewed_are_public
        )

    def full_url(self) -> RelativeAbsoluteUrl:
        if self.is_usable:
            if self.file:
                return AutoAbsoluteUrl(self.file.url)
            elif self.remote_url:
                return AutoAbsoluteUrl(f"/proxy/emoji/{self.pk}/")
        return StaticAbsoluteUrl("img/blank-emoji-128.png")

    def as_html(self):
        if self.is_usable:
            return mark_safe(
                f'<img src="{self.full_url().relative}" class="emoji" alt="Emoji {self.shortcode}">'
            )
        return self.fullcode

    @classmethod
    def imageify_emojis(
        cls,
        content: str,
        *,
        emojis: list["Emoji"] | EmojiQuerySet | None = None,
        include_local: bool = True,
    ):
        """
        Find :emoji: in content and convert to <img>. If include_local is True,
        the local emoji will be used as a fallback for any shortcodes not defined
        by emojis.
        """
        emoji_set = (
            cast(list[Emoji], list(cls.locals.values())) if include_local else []
        )

        if emojis:
            if isinstance(emojis, (EmojiQuerySet, list)):
                emoji_set.extend(list(emojis))
            else:
                raise TypeError("Unsupported type for emojis")

        possible_matches = {
            emoji.shortcode: emoji.as_html() for emoji in emoji_set if emoji.is_usable
        }

        def replacer(match):
            fullcode = match.group(1).lower()
            if fullcode in possible_matches:
                return possible_matches[fullcode]
            return match.group()

        return mark_safe(Emoji.emoji_regex.sub(replacer, content))

    @classmethod
    def emojis_from_content(cls, content: str, domain: Domain | None) -> list[str]:
        """
        Return a parsed and sanitized of emoji found in content without
        the surrounding ':'.
        """
        emoji_hits = cls.emoji_regex.findall(strip_html(content))
        emojis = sorted({emoji.lower() for emoji in emoji_hits})
        return list(
            cls.objects.filter(local=(domain is None) or domain.local)
            .usable(domain)
            .filter(shortcode__in=emojis)
        )

    def to_ap_tag(self):
        """
        Return this Emoji as an ActivityPub Tag
        """
        return {
            "id": self.object_uri or f"https://{settings.MAIN_DOMAIN}/emoji/{self.pk}/",
            "type": "Emoji",
            "name": self.shortcode,
            "icon": {
                "type": "Image",
                "mediaType": self.mimetype,
                "url": self.full_url().absolute,
            },
        }

    @classmethod
    def by_ap_tag(cls, domain: Domain, data: dict, create: bool = False):
        """ """
        try:
            return cls.objects.get(object_uri=data["id"])
        except cls.DoesNotExist:
            if not create:
                raise KeyError(f"No emoji with ID {data['id']}", data)

        # create
        shortcode = data["name"].lower().strip(":")
        icon = data["icon"]
        category = (icon.get("category") or "")[:100]
        emoji = cls.objects.create(
            shortcode=shortcode,
            domain=None if domain.local else domain,
            local=domain.local,
            object_uri=data["id"],
            mimetype=icon["mediaType"],
            category=category,
            remote_url=icon["url"],
        )
        return emoji

    ### Mastodon API ###

    def to_mastodon_json(self):
        url = self.full_url().absolute
        data = {
            "shortcode": self.shortcode,
            "url": url,
            "static_url": self.remote_url or url,
            "visible_in_picker": self.public,
            "category": self.category or "",
        }
        return data