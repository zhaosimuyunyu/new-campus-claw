"""Extract HTML deliverables without interpreting ordinary prose as a page."""
import html
import re

from django.core.files.base import ContentFile

from .models import AgentArtifact


FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})([^\r\n]*)\r?\n", re.M)
DOCUMENT_START = re.compile(r"(?:<!doctype\s+html\b[^>]*>\s*)?<html\b[^>]*>", re.I)
DOCUMENT_TOKEN = re.compile(r"<!--[\s\S]*?-->|<script\b[^>]*>[\s\S]*?</script\s*>|<style\b[^>]*>[\s\S]*?</style\s*>|</html\s*>", re.I)


def extract_html(source):
    """Return display text and ordered, exact HTML sources. Ignore other fences."""
    spans = []

    def documents(start, end):
        cursor = start
        while match := DOCUMENT_START.search(source, cursor, end):
            closing = next((token for token in DOCUMENT_TOKEN.finditer(source, match.end(), end)
                            if token.group().lower().startswith("</html")), None)
            if closing is None:
                break
            spans.append((match.start(), closing.end(), source[match.start():closing.end()]))
            cursor = closing.end()

    cursor = 0
    while opening := FENCE.search(source, cursor):
        documents(cursor, opening.start())
        marker, language = opening.groups()
        closing = re.compile(r"^ {0,3}" + re.escape(marker[0]) + "{" + str(len(marker)) + r",}[ \t]*\r?$", re.M).search(source, opening.end())
        if closing is None:
            cursor = len(source)
            break
        if language.strip().lower() == "html":
            body = source[opening.end():closing.start()]
            # The newline immediately preceding the fence belongs to Markdown.
            body = re.sub(r"\r?\n$", "", body)
            if body.strip():
                spans.append((opening.start(), closing.end(), body))
        cursor = closing.end()
    documents(cursor, len(source))
    pieces, cursor = [], 0
    for start, end, _ in spans:
        pieces.append(source[cursor:start])
        pieces.append("\n\n")
        cursor = end
    pieces.append(source[cursor:])
    return "".join(pieces).strip(), [body for _, _, body in spans]


def html_filename(source, message_id, index):
    title = re.search(r"<title\b[^>]*>([\s\S]*?)</title\s*>", source, re.I)
    name = html.unescape(re.sub(r"<[^>]*>", "", title[1])) if title else ""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", name).strip(" .")[:100]
    if not name or re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", name, re.I):
        name = f"campusclaw-{message_id}-{index}"
    return name + ".html"


def save_html_artifact(owner, thread, source, message_id, index):
    name = html_filename(source, message_id, index)
    artifact = AgentArtifact(owner=owner, thread=thread, course_id=thread.course_id,
                             name=name, mime_type="text/html", preview_html=source)
    artifact.file.save(name, ContentFile(source.encode("utf-8")), save=True)
    return artifact
