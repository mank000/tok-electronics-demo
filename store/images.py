import io

from PIL import Image, ImageOps
from django.core.files.base import ContentFile


def open_raster(upload):
    """Load an uploaded image with its camera orientation and alpha channel intact."""
    upload.open()
    with Image.open(upload) as source:
        image = ImageOps.exif_transpose(source)
        image.load()
        if "A" in image.getbands() or "transparency" in image.info:
            return image.convert("RGBA")
        return image.convert("RGB")


def webp_content(image, max_size, quality=82):
    resized = image.copy()
    resized.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
    output = io.BytesIO()
    resized.save(output, format="WEBP", quality=quality, method=5)
    return ContentFile(output.getvalue())
