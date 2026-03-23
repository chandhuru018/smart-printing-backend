from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Dict, List, Tuple

try:
    import fitz
except ImportError:
    fitz = None

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    Image = None
    ImageDraw = None
    ImageFont = None

try:
    from docx import Document
except ImportError:
    Document = None


class DocumentProcessingError(Exception):
    pass


@dataclass
class PageMetrics:
    page_number: int
    color_percentage: float
    grayscale_ratio: float
    color_density: float
    text_ratio: float
    image_ratio: float
    estimated_print_time_sec: float


def _calculate_color_metrics(image: Image.Image) -> Tuple[float, float, float]:
    rgb = image.convert("RGB")
    src_w, src_h = rgb.size
    max_dim = max(src_w, src_h)

    # Downsample large pages for stable, faster color analysis.
    if max_dim > 900:
        scale = 900 / max_dim
        new_size = (max(1, int(src_w * scale)), max(1, int(src_h * scale)))
        if hasattr(Image, "Resampling"):
            rgb = rgb.resize(new_size, Image.Resampling.BILINEAR)
        else:
            rgb = rgb.resize(new_size)

    pixels = list(rgb.getdata())
    total = max(len(pixels), 1)

    informative_pixels = 0
    color_pixels = 0
    grayscale_pixels = 0
    density_sum = 0.0

    for r, g, b in pixels:
        max_channel = max(r, g, b)
        min_channel = min(r, g, b)
        diff = max_channel - min_channel
        value = max_channel / 255.0
        saturation = (diff / max_channel) if max_channel else 0.0

        # Ignore blank paper/background area.
        if value > 0.97 and saturation < 0.08:
            continue

        informative_pixels += 1
        is_color = diff > 18 and saturation > 0.12
        if is_color:
            color_pixels += 1
            density_sum += (1 - value) * (0.55 + (0.45 * saturation))
        else:
            grayscale_pixels += 1
            density_sum += (1 - value) * 0.45

    if informative_pixels == 0:
        return 0.0, 1.0, 0.01

    color_percentage = (color_pixels / informative_pixels) * 100
    grayscale_ratio = grayscale_pixels / informative_pixels
    color_density = density_sum / informative_pixels
    return round(color_percentage, 4), round(grayscale_ratio, 4), max(0.01, min(1.0, round(color_density, 4)))


def _estimate_page_text_ratio(image: Image.Image, text_hint: float = 0.0) -> float:
    gray = image.convert("L")
    histogram = gray.histogram()
    dark_pixels = sum(histogram[:80])
    mid_pixels = sum(histogram[80:180])
    total = max(sum(histogram), 1)

    structural_text_ratio = min(1.0, (dark_pixels + (0.35 * mid_pixels)) / total)
    combined = min(1.0, max(0.0, (0.65 * structural_text_ratio) + (0.35 * text_hint)))
    return combined


def _estimate_print_time(color_density: float, text_ratio: float) -> float:
    base = 2.8
    color_penalty = color_density * 3.2
    image_penalty = (1 - text_ratio) * 2.6
    return round(base + color_penalty + image_penalty, 2)


def _pdf_to_images(file_bytes: bytes) -> Tuple[List[Image.Image], List[float]]:
    if fitz is None or Image is None:
        raise DocumentProcessingError("Missing dependencies for PDF analysis. Install PyMuPDF and Pillow.")
    doc = fitz.open(stream=file_bytes, filetype="pdf")
    images = []
    text_hints = []

    for page in doc:
        pix = page.get_pixmap(dpi=120, alpha=False)
        img = Image.open(io.BytesIO(pix.tobytes("png")))
        images.append(img)
        text = page.get_text("text") or ""
        text_hints.append(min(1.0, len(text.strip()) / 2500))

    doc.close()
    return images, text_hints


def _docx_to_images(file_bytes: bytes) -> Tuple[List[Image.Image], List[float]]:
    if Document is None or Image is None or ImageDraw is None or ImageFont is None:
        raise DocumentProcessingError("Missing dependencies for DOCX analysis. Install python-docx and Pillow.")
    doc = Document(io.BytesIO(file_bytes))
    paragraphs = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
    if not paragraphs:
        paragraphs = ["(Blank document)"]

    full_text = "\n".join(paragraphs)
    chunks = [full_text[i : i + 2600] for i in range(0, len(full_text), 2600)]

    pages = []
    hints = []
    width, height = 1240, 1754

    try:
        font = ImageFont.truetype("arial.ttf", 28)
    except OSError:
        font = ImageFont.load_default()

    for chunk in chunks:
        img = Image.new("RGB", (width, height), color="white")
        draw = ImageDraw.Draw(img)
        y = 100
        for line in chunk.splitlines() or [chunk]:
            wrapped = [line[i : i + 85] for i in range(0, len(line), 85)] or [""]
            for piece in wrapped:
                draw.text((80, y), piece, fill="black", font=font)
                y += 36
                if y > height - 100:
                    break
            if y > height - 100:
                break
        pages.append(img)

        image_in_doc = len(doc.inline_shapes)
        image_hint = min(0.9, image_in_doc / max(1, len(paragraphs)))
        hints.append(max(0.2, 1.0 - image_hint))

    return pages, hints


def _image_to_page(file_bytes: bytes) -> Tuple[List[Image.Image], List[float]]:
    if Image is None:
        raise DocumentProcessingError("Missing dependency for image analysis. Install Pillow.")
    image = Image.open(io.BytesIO(file_bytes)).convert("RGB")
    return [image], [0.2]


def analyze_document(file_bytes: bytes, filename: str) -> Dict:
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    try:
        if extension == "pdf":
            pages, hints = _pdf_to_images(file_bytes)
        elif extension == "docx":
            pages, hints = _docx_to_images(file_bytes)
        elif extension in {"jpg", "jpeg", "png"}:
            pages, hints = _image_to_page(file_bytes)
        else:
            raise DocumentProcessingError("Unsupported file type")

        metrics: List[PageMetrics] = []
        total_color_density = 0.0

        for idx, image in enumerate(pages, start=1):
            color_percentage, grayscale_ratio, color_density = _calculate_color_metrics(image)
            text_ratio = _estimate_page_text_ratio(image, text_hint=hints[idx - 1])
            image_ratio = 1 - text_ratio
            estimated_print_time = _estimate_print_time(color_density=color_density, text_ratio=text_ratio)

            metrics.append(
                PageMetrics(
                    page_number=idx,
                    color_percentage=round(color_percentage, 2),
                    grayscale_ratio=round(grayscale_ratio, 4),
                    color_density=round(color_density, 4),
                    text_ratio=round(text_ratio, 4),
                    image_ratio=round(image_ratio, 4),
                    estimated_print_time_sec=estimated_print_time,
                )
            )
            total_color_density += color_density

        total_pages = len(metrics)
        overall_density = round(total_color_density / max(1, total_pages), 4)
        # Hybrid threshold: catches chart/logo pages without over-marking grayscale-heavy pages.
        color_pages = sum(
            1
            for m in metrics
            if (m.color_percentage >= 1.0) or (m.color_percentage >= 0.4 and m.color_density >= 0.06)
        )
        bw_pages = total_pages - color_pages
        total_estimated_time = round(sum(m.estimated_print_time_sec for m in metrics), 2)

        return {
            "page_count": total_pages,
            "color_pages": color_pages,
            "bw_pages": bw_pages,
            "overall_color_density": overall_density,
            "total_estimated_print_time_sec": total_estimated_time,
            "page_metrics": [m.__dict__ for m in metrics],
        }
    except Exception as exc:
        raise DocumentProcessingError(str(exc)) from exc
