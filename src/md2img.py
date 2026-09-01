# -*- coding: utf-8 -*-
"""
===================================
Markdown 转图片工具模块
===================================

将 Markdown 转为 PNG 图片（用于不支持 Markdown 的通知渠道）。
支持 wkhtmltoimage (imgkit) 与 markdown-to-file (m2f)，后者对 emoji 支持更好 (Issue #455)。

Security note: imgkit passes HTML to wkhtmltoimage via stdin, not argv, so
command injection from content is not applicable. Output is rasterized to PNG
(no script execution). Input is from system-generated reports, not raw user
input. Risk is considered low for the current use case.
"""

import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Mapping, Optional

from src.share_image import (
    ShareImageBranding,
    build_share_image_html,
    share_image_branding_from_config,
)

logger = logging.getLogger(__name__)


def _share_image_branding(config: object) -> ShareImageBranding:
    return share_image_branding_from_config(config)


def _resolve_playwright_command() -> Optional[str]:
    """Resolve a global or repository-local Playwright CLI executable."""
    command = shutil.which("playwright")
    if command:
        return command

    repository_root = Path(__file__).resolve().parent.parent
    executable_name = "playwright.cmd" if os.name == "nt" else "playwright"
    local_command = repository_root / "apps" / "dsa-web" / "node_modules" / ".bin" / executable_name
    if local_command.is_file():
        return str(local_command)
    return None


def _markdown_to_image_playwright(
    markdown_text: str,
    structured_payload: Optional[Mapping[str, Any]] = None,
    branding: Optional[ShareImageBranding] = None,
) -> Optional[bytes]:
    """Convert a share-poster HTML document to PNG with Playwright Chromium."""
    playwright_command = _resolve_playwright_command()
    if playwright_command is None:
        logger.warning(
            "Playwright CLI not found. Install Web dependencies with: "
            "cd apps/dsa-web && npm ci. Fallback to text."
        )
        return None

    temp_dir = tempfile.mkdtemp()
    html_path = Path(temp_dir) / "report.html"
    png_path = Path(temp_dir) / "report.png"
    try:
        html_path.write_text(
            build_share_image_html(
                markdown_text,
                structured_payload=structured_payload,
                branding=branding,
            ),
            encoding="utf-8",
        )
        result = subprocess.run(
            [
                playwright_command,
                "screenshot",
                "--browser",
                "chromium",
                "--viewport-size",
                "1080,720",
                "--full-page",
                html_path.resolve().as_uri(),
                str(png_path),
            ],
            capture_output=True,
            timeout=60,
            check=False,
        )
        if result.returncode == 0 and png_path.exists():
            return png_path.read_bytes()

        stderr = result.stderr.decode("utf-8", errors="replace")
        logger.error("Playwright image conversion failed: %s", stderr[:500])
        return None
    except Exception as exc:
        logger.error("Playwright image conversion error: %s", exc)
        return None
    finally:
        try:
            shutil.rmtree(temp_dir)
        except Exception as exc:
            logger.debug("Failed to remove temp dir %s: %s", temp_dir, exc)


def _markdown_to_image_m2f(
    markdown_text: str,
    structured_payload: Optional[Mapping[str, Any]] = None,
    branding: Optional[ShareImageBranding] = None,
) -> Optional[bytes]:
    """Convert Markdown to PNG via markdown-to-file (m2f) CLI. Better emoji support (Issue #455)."""
    m2f_command = shutil.which("m2f")
    if m2f_command is None:
        logger.warning(
            "m2f (markdown-to-file) not found in PATH. "
            "Install with: npm i -g markdown-to-file. Fallback to text."
        )
        return None

    temp_dir = None
    try:
        temp_dir = tempfile.mkdtemp()
        md_path = os.path.join(temp_dir, "report.md")
        with open(md_path, "w", encoding="utf-8") as f:
            # m2f keeps raw HTML in Markdown input, allowing both engines to
            # share the same deterministic poster layout and embedded QR codes.
            f.write(
                build_share_image_html(
                    markdown_text,
                    structured_payload=structured_payload,
                    branding=branding,
                )
            )

        result = subprocess.run(
            [m2f_command, md_path, "png", f"outputDirectory={temp_dir}"],
            capture_output=True,
            timeout=60,
            check=False,
        )
        png_path = os.path.join(temp_dir, "report.png")
        if result.returncode != 0 or not os.path.isfile(png_path):
            logger.warning(
                "m2f conversion failed: returncode=%s, stderr=%s",
                result.returncode,
                (result.stderr or b"").decode("utf-8", errors="replace")[:200],
            )
            return None

        with open(png_path, "rb") as f:
            return f.read()
    except subprocess.TimeoutExpired:
        logger.warning("m2f conversion timed out (60s)")
        return None
    except Exception as e:
        logger.warning("markdown_to_image (m2f) failed: %s", e)
        return None
    finally:
        if temp_dir and os.path.isdir(temp_dir):
            try:
                shutil.rmtree(temp_dir)
            except OSError as e:
                logger.debug("Failed to remove temp dir %s: %s", temp_dir, e)


def _markdown_to_image_wkhtml(
    markdown_text: str,
    structured_payload: Optional[Mapping[str, Any]] = None,
    branding: Optional[ShareImageBranding] = None,
) -> Optional[bytes]:
    """Convert Markdown to PNG via imgkit/wkhtmltoimage."""
    try:
        import imgkit
    except ImportError:
        logger.debug("imgkit not installed, markdown_to_image unavailable")
        return None

    try:
        html = build_share_image_html(
            markdown_text,
            structured_payload=structured_payload,
            branding=branding,
        )
        options = {
            "format": "png",
            "encoding": "UTF-8",
            "width": 1080,
            "disable-smart-width": "",
            "quality": 95,
            "quiet": "",
        }
        out = imgkit.from_string(html, False, options=options)
        if out and isinstance(out, bytes) and len(out) > 0:
            return out
        logger.warning("imgkit.from_string returned empty or invalid result")
        return None
    except OSError as e:
        if "wkhtmltoimage" in str(e).lower() or "wkhtmltopdf" in str(e).lower():
            logger.debug("wkhtmltopdf/wkhtmltoimage not found: %s", e)
        else:
            logger.warning("imgkit/wkhtmltoimage error: %s", e)
        return None
    except Exception as e:
        logger.warning("markdown_to_image conversion failed: %s", e)
        return None


def _resolve_browser_command() -> Optional[str]:
    """Resolve a system-installed Chromium-family browser (Edge/Chrome)."""
    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return None


def _trim_blank_bottom(image_bytes: bytes, *, threshold: int = 232) -> bytes:
    """Trim near-uniform bottom padding from a screenshot (poster bg ~#eef4fd)."""
    try:
        from PIL import Image
        import io

        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        width, height = image.size
        pixels = image.load()
        bottom = height
        for y in range(height - 1, -1, -1):
            row_blank = True
            for x in range(0, width, 7):
                r, g, b = pixels[x, y]
                # 背景 #eef4fd 各通道都较高，纯白内容区域更高；任何明显暗像素都算内容
                if r < threshold or g < threshold or b < threshold:
                    row_blank = False
                    break
            if not row_blank:
                bottom = y + 1
                break
        if bottom < height:
            image = image.crop((0, 0, width, bottom))
        output = io.BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()
    except Exception:
        return image_bytes


def _markdown_to_image_edge(
    markdown_text: str,
    structured_payload: Optional[Mapping[str, Any]] = None,
    branding: Optional[ShareImageBranding] = None,
) -> Optional[bytes]:
    """Convert the share-poster HTML to PNG via a system Chromium (Edge/Chrome)
    headless screenshot. Zero-download fallback for environments where
    wkhtmltoimage / playwright browsers are not installed."""
    browser = _resolve_browser_command()
    if browser is None:
        logger.warning("Edge/Chrome not found; markdown_to_image (edge) unavailable")
        return None

    temp_dir = tempfile.mkdtemp()
    html_path = Path(temp_dir) / "report.html"
    png_path = Path(temp_dir) / "report.png"
    try:
        html_path.write_text(
            build_share_image_html(
                markdown_text,
                structured_payload=structured_payload,
                branding=branding,
            ),
            encoding="utf-8",
        )
        result_bytes: Optional[bytes] = None
        for height in (1920, 2600, 3600, 5200):
            try:
                subprocess.run(
                    [
                        browser,
                        "--headless=new",
                        "--disable-gpu",
                        "--no-first-run",
                        "--hide-scrollbars",
                        f"--window-size=1080,{height}",
                        "--force-device-scale-factor=2",
                        f"--screenshot={png_path}",
                        html_path.resolve().as_uri(),
                    ],
                    capture_output=True,
                    timeout=90,
                    check=False,
                )
            except Exception as exc:
                logger.error("Edge screenshot error: %s", exc)
                return None
            if not png_path.exists() or png_path.stat().st_size == 0:
                continue
            trimmed = _trim_blank_bottom(png_path.read_bytes())
            result_bytes = trimmed
            try:
                from PIL import Image
                import io

                with Image.open(io.BytesIO(trimmed)) as image:
                    trimmed_height = image.height
            except Exception:
                return trimmed
            # 若裁剪后仍接近满高，说明内容可能超出视口，用更高一档重试
            if trimmed_height < height * 2 * 0.95:
                return trimmed
        return result_bytes
    except Exception as exc:
        logger.error("Edge image conversion error: %s", exc)
        return None
    finally:
        try:
            shutil.rmtree(temp_dir)
        except Exception:
            pass


def markdown_to_image(
    markdown_text: str,
    max_chars: int = 15000,
    structured_payload: Optional[Mapping[str, Any]] = None,
) -> Optional[bytes]:
    """
    Convert Markdown to PNG image bytes.

    Engine is read from config.md2img_engine: wkhtmltoimage (default),
    markdown-to-file, or playwright.

    When conversion fails or dependencies unavailable, returns None so caller
    can fall back to text sending.

    Args:
        markdown_text: Raw Markdown content.
        max_chars: Skip conversion and return None if content exceeds this length
            (avoids huge images). Default 15000.
        structured_payload: Optional stock-analysis or market-review JSON. Exact
            structured fields take precedence over Markdown extraction.

    Returns:
        PNG bytes, or None if conversion fails or dependencies unavailable.
    """
    if len(markdown_text) > max_chars:
        logger.warning(
            "Markdown content (%d chars) exceeds max_chars (%d), skipping image conversion",
            len(markdown_text),
            max_chars,
        )
        return None

    try:
        from src.config import get_config

        config = get_config()
        engine = getattr(config, "md2img_engine", "wkhtmltoimage")
        branding = _share_image_branding(config)
    except Exception:
        engine = "wkhtmltoimage"
        branding = share_image_branding_from_config(object())

    if engine == "markdown-to-file":
        result = _markdown_to_image_m2f(markdown_text, structured_payload, branding)
    elif engine == "playwright":
        result = _markdown_to_image_playwright(markdown_text, structured_payload, branding)
    elif engine == "edge":
        result = _markdown_to_image_edge(markdown_text, structured_payload, branding)
    else:
        result = _markdown_to_image_wkhtml(markdown_text, structured_payload, branding)

    # 自动回退：wkhtmltoimage / playwright / m2f 依赖外部二进制，未安装时
    # 用系统自带 Edge/Chrome headless 渲染（零下载、效果一致）。
    if result is None:
        result = _markdown_to_image_edge(markdown_text, structured_payload, branding)
    return result
