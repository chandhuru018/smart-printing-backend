import os
import shutil
import subprocess
import tempfile
import time
import io
import textwrap
from pathlib import Path

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


class PrinterOfflineError(Exception):
    pass


class PrintExecutionError(Exception):
    pass


class PrintService:
    def __init__(self, printer_name: str | None = None):
        self.printer_name = printer_name or os.getenv("PRINTER_NAME")
        self.color_printer_name = os.getenv("PRINTER_COLOR_NAME", "").strip() or None
        self.bw_printer_name = os.getenv("PRINTER_BW_NAME", "").strip() or None
        self.strict_color_enforcement = os.getenv("STRICT_COLOR_ENFORCEMENT", "false").lower() == "true"
        self.simulate_without_hardware = os.getenv("SIMULATE_PRINT", "true").lower() == "true"
        self._resolved_windows_printer: str | None = None

    def _preferred_printer_for_mode(self, mode: str) -> str | None:
        if mode == "color" and self.color_printer_name:
            return self.color_printer_name
        if mode == "bw" and self.bw_printer_name:
            return self.bw_printer_name
        return self.printer_name

    def _expand_page_ranges(self, page_ranges: str, total_pages: int) -> list[int]:
        normalized = (page_ranges or "all").strip().lower()
        if normalized in {"", "all", "*"}:
            return list(range(1, total_pages + 1))

        pages: set[int] = set()
        for chunk in normalized.split(","):
            part = chunk.strip()
            if not part:
                continue
            if "-" in part:
                start_str, end_str = part.split("-", 1)
                start = int(start_str)
                end = int(end_str)
                if start > end:
                    start, end = end, start
                if start < 1 or end > total_pages:
                    raise PrintExecutionError("Page range is out of document bounds")
                pages.update(range(start, end + 1))
            else:
                page_num = int(part)
                if page_num < 1 or page_num > total_pages:
                    raise PrintExecutionError("Page number is out of document bounds")
                pages.add(page_num)

        if not pages:
            raise PrintExecutionError("No pages selected for printing")
        return sorted(pages)

    def _sumatra_path(self) -> str | None:
        configured = os.getenv("SUMATRA_PDF_PATH", "").strip()
        if configured and Path(configured).exists():
            return configured

        if shutil.which("SumatraPDF"):
            return "SumatraPDF"

        common_paths = [
            r"C:\Program Files\SumatraPDF\SumatraPDF.exe",
            r"C:\Program Files (x86)\SumatraPDF\SumatraPDF.exe",
        ]
        for path in common_paths:
            if Path(path).exists():
                return path
        return None

    def _resolve_windows_printer_name(self, mode: str) -> str | None:
        if os.name != "nt":
            return None
        preferred = self._preferred_printer_for_mode(mode=mode)
        if preferred:
            return preferred
        if self._resolved_windows_printer:
            return self._resolved_windows_printer

        if mode == "color":
            script = (
                "$printers = Get-Printer | Where-Object { $_.Name -notmatch 'OneNote|PDF|XPS|Fax' }; "
                "$p = $printers | Where-Object { $_.Default -eq $true -and $_.Name -notmatch 'Universal Print Driver|Grayscale' } | Select-Object -First 1; "
                "if (-not $p) { "
                "  $p = $printers | Where-Object { $_.Name -match 'EPSON L|EPSON ET|EPSON WF|EPSON' -and $_.Name -notmatch 'Universal Print Driver|Grayscale' } | Select-Object -First 1 "
                "}; "
                "if (-not $p) { "
                "  $p = $printers | Where-Object { $_.Name -notmatch 'Universal Print Driver|Grayscale' } | Select-Object -First 1 "
                "}; "
                "if (-not $p) { $p = $printers | Select-Object -First 1 }; "
                "if ($p) { Write-Output $p.Name }"
            )
        else:
            script = (
                "$printers = Get-Printer | Where-Object { $_.Name -notmatch 'OneNote|PDF|XPS|Fax' }; "
                "$p = $printers | Where-Object { $_.Default -eq $true } | Select-Object -First 1; "
                "if (-not $p) { "
                "  $p = $printers | Where-Object { $_.Name -match 'EPSON|HP|Canon|Brother|Xerox|Kyocera' } | Select-Object -First 1 "
                "}; "
                "if (-not $p) { $p = $printers | Select-Object -First 1 }; "
                "if ($p) { Write-Output $p.Name }"
            )
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-Command", script],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            name = (proc.stdout or "").strip()
            if name:
                self._resolved_windows_printer = name
                return name
        except Exception:
            pass
        return None

    def _build_pdf_for_printing(self, file_bytes: bytes, mode: str, page_ranges: str) -> str:
        if fitz is None:
            raise PrintExecutionError("PDF page/mode print options require PyMuPDF.")

        source = fitz.open(stream=file_bytes, filetype="pdf")
        selected_pages = self._expand_page_ranges(page_ranges=page_ranges, total_pages=source.page_count)
        output = fitz.open()

        try:
            if mode == "bw":
                for page_number in selected_pages:
                    page = source.load_page(page_number - 1)
                    pix = page.get_pixmap(colorspace=fitz.csGRAY, dpi=220, alpha=False)
                    # Keep original PDF page size/orientation; only convert content to grayscale.
                    out_page = output.new_page(width=page.rect.width, height=page.rect.height)
                    out_page.insert_image(out_page.rect, pixmap=pix, keep_proportion=False)
            else:
                for page_number in selected_pages:
                    output.insert_pdf(source, from_page=page_number - 1, to_page=page_number - 1)

            fd, path = tempfile.mkstemp(suffix=".pdf")
            os.close(fd)
            output.save(path)
            return path
        finally:
            output.close()
            source.close()

    def _build_image_pdf_for_printing(self, file_bytes: bytes, mode: str) -> str:
        if Image is None:
            raise PrintExecutionError("Image printing needs Pillow.")
        try:
            image = Image.open(io.BytesIO(file_bytes)).convert("RGB")
            if mode == "bw":
                image = image.convert("L").convert("RGB")

            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
                image.save(tmp.name, "PDF", resolution=150)
                return tmp.name
        except Exception as exc:
            raise PrintExecutionError(f"Failed to prepare image for printing: {exc}") from exc

    def _build_docx_pdf_for_printing(self, file_bytes: bytes) -> str:
        if Document is None or Image is None or ImageDraw is None or ImageFont is None:
            raise PrintExecutionError("DOCX fallback printing needs python-docx and Pillow.")

        try:
            doc = Document(io.BytesIO(file_bytes))
            lines = [p.text.strip() for p in doc.paragraphs if p.text and p.text.strip()]
            if not lines:
                lines = ["(Blank document)"]

            width, height = 1240, 1754
            margin_x = 80
            top_margin = 100
            bottom_margin = 100
            line_height = 36
            max_chars = 85

            try:
                font = ImageFont.truetype("arial.ttf", 28)
            except OSError:
                font = ImageFont.load_default()

            pages = []
            current = Image.new("RGB", (width, height), color="white")
            draw = ImageDraw.Draw(current)
            y = top_margin

            for line in lines:
                wrapped = textwrap.wrap(line, width=max_chars) or [""]
                for piece in wrapped:
                    if y > (height - bottom_margin):
                        pages.append(current)
                        current = Image.new("RGB", (width, height), color="white")
                        draw = ImageDraw.Draw(current)
                        y = top_margin
                    draw.text((margin_x, y), piece, fill="black", font=font)
                    y += line_height

            pages.append(current)
            first, rest = pages[0], pages[1:]
            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
                first.save(tmp.name, "PDF", resolution=150, save_all=True, append_images=rest)
                return tmp.name
        except Exception as exc:
            raise PrintExecutionError(f"Failed to prepare DOCX for printing: {exc}") from exc

    def _prepare_file_for_printing(self, file_bytes: bytes, filename: str, mode: str, page_ranges: str) -> tuple[str, str]:
        extension = (Path(filename).suffix or "").lower()

        if extension == ".pdf":
            # Page ranges are materialized into a temporary PDF, so downstream command should print all pages.
            return self._build_pdf_for_printing(file_bytes=file_bytes, mode=mode, page_ranges=page_ranges), "all"

        if extension in {".jpg", ".jpeg", ".png"}:
            if (page_ranges or "").strip().lower() not in {"", "all", "*"}:
                raise PrintExecutionError("Custom page ranges are currently supported for PDF files only.")
            return self._build_image_pdf_for_printing(file_bytes=file_bytes, mode=mode), "all"

        if extension == ".docx":
            if (page_ranges or "").strip().lower() not in {"", "all", "*"}:
                raise PrintExecutionError("Custom page ranges are currently supported for PDF files only.")
            try:
                return self._build_docx_pdf_for_printing(file_bytes=file_bytes), "all"
            except PrintExecutionError:
                # Fall back to native app printing when DOCX-to-PDF conversion cannot run.
                pass

        if (page_ranges or "").strip().lower() not in {"", "all", "*"}:
            raise PrintExecutionError("Custom page ranges are currently supported for PDF files only.")

        suffix = extension or ".dat"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(file_bytes)
            return tmp.name, page_ranges

    def _apply_windows_color_preference(self, mode: str):
        if os.name != "nt":
            return

        target_printer = self._resolve_windows_printer_name(mode=mode)
        if not target_printer:
            if mode == "color" and self.strict_color_enforcement:
                raise PrintExecutionError("No target printer detected to enforce color mode.")
            return

        escaped_printer = target_printer.replace("'", "''")
        color_flag = "$true" if mode == "color" else "$false"
        script = (
            "$ErrorActionPreference='Stop'; "
            "Import-Module PrintManagement -ErrorAction SilentlyContinue; "
            "if (Get-Command Set-PrintConfiguration -ErrorAction SilentlyContinue) { "
            f"Set-PrintConfiguration -PrinterName '{escaped_printer}' -Color {color_flag} | Out-Null"
            f"; $cfg = Get-PrintConfiguration -PrinterName '{escaped_printer}'; "
            f"if ($cfg.Color -ne {color_flag}) {{ throw 'Unable to apply requested color mode on printer.' }}"
            " } else { throw 'Set-PrintConfiguration command unavailable.' }"
        )

        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-Command", script],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            if proc.returncode != 0 and mode == "color" and self.strict_color_enforcement:
                # For color jobs we should not silently continue in BW when preference cannot be applied.
                raise PrintExecutionError(proc.stderr.strip() or "Failed to set printer color mode")
        except Exception:
            if mode == "color" and self.strict_color_enforcement:
                raise

    def _resolve_print_command(self, file_path: str, mode: str, page_ranges: str):
        target_printer = self._resolve_windows_printer_name(mode=mode)

        if shutil.which("lp"):
            command = ["lp"]
            if target_printer:
                command.extend(["-d", target_printer])
            if mode == "bw":
                command.extend(["-o", "ColorModel=Gray"])
            normalized_ranges = (page_ranges or "all").strip().lower()
            if normalized_ranges not in {"", "all", "*"}:
                command.extend(["-P", page_ranges])
            command.append(file_path)
            return command

        if shutil.which("lpr"):
            command = ["lpr"]
            if target_printer:
                command.extend(["-P", target_printer])
            command.append(file_path)
            return command

        if os.name == "nt":
            sumatra = self._sumatra_path()
            if sumatra and Path(file_path).suffix.lower() == ".pdf":
                settings: list[str] = []
                normalized_ranges = (page_ranges or "all").strip().lower()
                if normalized_ranges not in {"", "all", "*"}:
                    settings.append(page_ranges)
                settings.append("fit")
                settings.append("monochrome" if mode == "bw" else "color")
                settings_arg = ",".join(settings)

                command = [sumatra, "-silent", "-print-to-default"]
                if target_printer:
                    command = [sumatra, "-silent", "-print-to", target_printer]
                if settings_arg:
                    command.extend(["-print-settings", settings_arg])
                command.append(file_path)
                return command

            # ── Adobe Acrobat / Reader Fallback ──
            if Path(file_path).suffix.lower() == ".pdf":
                adobe_paths = [
                    r"C:\Program Files (x86)\Adobe\Acrobat Reader DC\Reader\AcroRd32.exe",
                    r"C:\Program Files\Adobe\Acrobat DC\Acrobat\Acrobat.exe",
                    r"C:\Program Files (x86)\Adobe\Reader 11.0\Reader\AcroRd32.exe",
                    r"C:\Program Files (x86)\Adobe\Reader 10.0\Reader\AcroRd32.exe",
                ]
                acrobat = None
                for ap in adobe_paths:
                    if Path(ap).exists():
                        acrobat = ap
                        break
                
                if acrobat:
                    cmd = [acrobat, "/t", file_path]
                    if target_printer:
                        cmd.append(target_printer)
                    # Adobe typically leaves the background window open but executes the print immediately.
                    return cmd

            # ── Generic Shell Fallback ──
            escaped_path = file_path.replace("'", "''")
            escaped_printer = (target_printer or "").replace("'", "''")
            return [
                "powershell",
                "-Command",
                (
                    f"$path='{escaped_path}'; "
                    f"$printer='{escaped_printer}'; "
                    "if ($printer) { Start-Process -FilePath $path -Verb PrintTo -ArgumentList ('\"' + $printer + '\"') -PassThru | Out-Null } "
                    "else { Start-Process -FilePath $path -Verb Print -PassThru | Out-Null }"
                ),
            ]

        if self.simulate_without_hardware:
            return None
        raise PrinterOfflineError("No supported print command found (lp/lpr/print)")

    def print_file_bytes(self, file_bytes: bytes, filename: str, options: dict | None = None) -> dict:
        options = options or {}
        copies = max(1, int(options.get("copies", 1)))
        mode = options.get("mode", "bw")
        if mode not in {"bw", "color"}:
            mode = "bw"
        page_ranges = (options.get("page_ranges") or "all").strip()

        tmp_path, effective_page_ranges = self._prepare_file_for_printing(
            file_bytes=file_bytes,
            filename=filename,
            mode=mode,
            page_ranges=page_ranges,
        )
        self._apply_windows_color_preference(mode=mode)
        command = self._resolve_print_command(file_path=tmp_path, mode=mode, page_ranges=effective_page_ranges)

        try:
            if command is None:
                return {
                    "command": "simulated-print",
                    "stdout": "Print simulated without hardware",
                    "status": "simulated",
                }

            stdout_chunks = []
            for _ in range(copies):
                proc = subprocess.run(command, capture_output=True, text=True, timeout=60)
                if proc.returncode != 0:
                    raise PrintExecutionError(proc.stderr.strip() or "Print command failed")
                if proc.stdout.strip():
                    stdout_chunks.append(proc.stdout.strip())
            return {
                "command": " ".join(command),
                "stdout": "\n".join(stdout_chunks),
                "status": "queued",
            }
        except subprocess.TimeoutExpired as exc:
            raise PrintExecutionError("Print command timeout") from exc
        finally:
            # Windows Print/PrintTo fallback may launch Acrobat asynchronously.
            # Keep temp file briefly so the spawned process can still open it.
            if (
                os.name == "nt"
                and command
                and isinstance(command, list)
                and command[0].lower() == "powershell"
            ):
                hold_seconds = int(os.getenv("WINDOWS_PRINT_FILE_HOLD_SECONDS", "20"))
                if hold_seconds > 0:
                    time.sleep(hold_seconds)
            try:
                os.remove(tmp_path)
            except OSError:
                pass
