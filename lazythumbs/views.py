from hashlib import md5
import json
import os
import re
import types

from django.conf import settings
from django.core.cache import cache
from django.core.files.storage import FileSystemStorage
from django.core.files.base import ContentFile
from django.core.exceptions import SuspiciousOperation
from django.http import HttpResponse
from django.views.generic.base import View
from PIL import ImageOps, Image

class LazyThumbRenderer(View):
    """
    Perform requested image render operations and handle fs logic and caching
    of 404s. Maps a requested action (currently 'thumbnail' and 'resize' are
    supported) to a matching method named after the action prefixed with
    'action_'. Once the argument signatures are relaxed one can implement new
    image transformations simply by subclassing this view and adding "action_"
    methods that return raw image data as a string.
    """
    fs = FileSystemStorage()
    def action(fun):
        """
        Decorator used to denote an instance method as an action: a function
        that takes a path to an image, performs PIL on it, and returns raw imag
        data.
        """
        fun.is_action = True
        return fun

    @property
    def _allowed_actions(self):
        """
        Builds internal list of allowed actions (methods denoted with
        @action)
        """
        return [
            a for a in dir(self)
            if (lambda x: type(x) == types.MethodType
                and getattr(x, 'is_action', False))(getattr(self, a))
        ]

    def get(self, request, action, geometry, source_path):
        """
        Perform action routing and handle sanitizing url input. Handles caching the path to a rendered image to
        django.cache and saves the new image on the filesystem. 404s are cached to
        save time the next time the missing image is requested.

        :param request: HttpRequest
        :param action: some action, eg thumbnail or resize
        :param geometry: a string of either '\dx\d' or just '\d'
        :param source_path: the fs path to the image to be manipulated
        :returns: an HttpResponse with an image/jpeg content_type
        """
        source_path = os.path.join(settings.THUMBNAIL_SOURCE_PATH, source_path)

        # reject naughty paths and actions
        if source_path.startswith('/'):
            return self.four_oh_four()
        if re.match('\.\./', source_path):
            return self.four_oh_four()
        if action not in self.allowed_actions:
            return self.four_oh_four()

        try:
            w,h = geometry.split('x')
        except ValueError:
            w = geometry
            h = None

        cache_key = self.cache_key(source_path, action, width, height)
        source_meta = cache.get(cache_key)

        if source_meta:
            source_meta = json.loads(img_meta)
            was_404 = source_meta['was_404']
            rendered_path = source_meta['rendered_path']
            rendered_path = os.path.join(settings.LAZYTHUMB_SOURCE_PATH, rendered_path)

            try:
                f = self.fs.open(rendered_path)
                raw_data = f.read()
            except IOError:
                _, raw_data = self.render_and_save(source_path, width, height)
            except SuspiciousOperation:
                return self.four_oh_four()
            finally:
                return self.two_hundred(raw_data)

        if self.fs.exists(source_path):
            rendered_path, raw_data = self.render_and_save(source_path, width, height)
            response = self.two_hundred(raw_data)
            source_meta = dict(rendered_path=rendered_path, was_404=False)
            expires = settings.LAZYTHUMB_CACHE_TIMEOUT
        else:
            response = self.four_hundred()
            source_meta = dict(rendered_path='', was_404=True)
            expires = settings.LAZYTHUMB_404_CACHE_TIMEOUT

        cache.set(cache_key, json.dumps(source_meta), expires)

        return response

    def render_and_save(self, img_path, width, height):
        """
        Defers to action_ methods to actually manipulate an image. Saves the
        resulting image to the filesystem.

        :returns rendered_path: fs path to new image
        :returns raw_data: raw data of new image as string
        """
        action_hash = self.hash_(img_path, action, width, height)
        rendered_path = '%s/%s/%s' % (action_hash[0:2], action_hash[2:4], action_hash)
        raw_data = getattr(self, action)(img_path, width, height)
        self.fs.save(rendered_path, ContentFile(raw_data))

        return rendered_path, raw_data

    def cache_key(self, img_path, action, width, height):
        """
        Compute a unique cache key for an image operation. Takes width, height,
        fs path, and desired action into account.

        :param img_path: fs path to a source image
        :param action: string representing image manipulation to occur
        :param width: integer width in pixels
        :param height: integer height in pixels
        """
        hashed = self.hash_(img_path, action, width, height)
        return 'lazythumbs:%s' % hashed

    def hash_(self, img_path, action, width, height):
        """
        Generate an md5 hash for an image operation. Takes width, height,
        fs path, and desired action into account.

        :param img_path: fs path to a source image
        :param action: string representing image manipulation to occur
        :param width: integer width in pixels
        :param height: integer height in pixels
        """
        hashed = md5('%s:%s:%s:%s' % img_path, action, width, height)
        return hashed.hexdigest()

    def two_hundered(img_data):
        """
        Generate a 200 image response with raw image data, Cache-Control set,
        and an image/jpeg content-type.

        :param img_data: raw image data as a string
        """
        resp = HttpResponse(img_data, content_type='image/jpeg')
        resp['Cache-Control'] = 'public,max-age=%s' % settings.THUMBNAIL_CACHE_TIMEOUT
        return resp

    def four_oh_four(self):
        """
        Generate a 404 response with an image/jpeg content_type. Sets a
        Cahce-Control header for caches like Akamai (browsers will ignore it
        since it's a 4xx.
        """
        resp = HttpResponse(status=404, content_type='image/jpeg')
        resp['Cache-Control'] = 'public,max-age=%s' % settings.THUMBNAIL_404_CACHE_TIMEOUT
        return resp

    @action
    def thumbnail(self, img_path, width, height=None):
        """
        Scale in one or two dimensions and then perform a left crop (if only
        one dimension was specified).

        :param img_path: path to an image to be thumbnailed
        :param width: width in pixels of desired thumbnail
        :param height: height in pixels of desired thumbnail (optional)

        :returns: raw image data as a string
        """
        img = Image.open(img)
        do_crop = False
        if height is None:
            do_crop = True
            height = int(img.size[1] * (float(width) / img.size[0]))
        width = min(width, img.size[1])
        height = min(height, img.size[0])

        img = img.resize((width, height), Image.ANTIALIAS)

        if do_crop:
            img = img.crop((0,0, width, width))

        return img.tostring()

    @action
    def resize(self, img_path, width, height):
        """
        resize to given dimenions.

        :param img_path: path to an image to be resizeed
        :param width: width in pixels of new image
        :param height: height in pixels of  new image

        :returns: raw image data as a string
        """
        img = Image.open(img)
        return img.resize((width, height), Image.ANTIALIAS).tostring()
