"""XMP sidecar writer for Lightroom: labels, hierarchical keywords, custom GDPR fields."""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from faceit_ai.integration.metadata_port import MetadataWriteRequest
from faceit_ai.metadata.keyword_builder import normalize_gdpr_reason, usage_keyword_token

_LOG = logging.getLogger("faceit_ai.xmp")

_NS_X = "adobe:ns:meta/"
_NS_RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
_NS_DC = "http://purl.org/dc/elements/1.1/"
_NS_XMP = "http://ns.adobe.com/xap/1.0/"
_NS_LR = "http://ns.adobe.com/lightroom/1.0/"
_NS_SOLA = "http://facit.ai/metadata/gdpr/1.0/"
_NS_XMLNS = "http://www.w3.org/2000/xmlns/"


def Q(uri: str, local: str) -> str:
    return f"{{{uri}}}{local}"


_RDF = Q(_NS_RDF, "RDF")
_RDF_DESC = Q(_NS_RDF, "Description")
_RDF_BAG = Q(_NS_RDF, "Bag")
_RDF_LI = Q(_NS_RDF, "li")
_XMPMETA = Q(_NS_X, "xmpmeta")


def _register_namespaces() -> None:
    ET.register_namespace("x", _NS_X)
    ET.register_namespace("rdf", _NS_RDF)
    ET.register_namespace("dc", _NS_DC)
    ET.register_namespace("xmp", _NS_XMP)
    ET.register_namespace("lr", _NS_LR)
    ET.register_namespace("sola", _NS_SOLA)


def sidecar_path_for_image(image_path: Path) -> Path:
    return image_path.with_suffix(".xmp")


def _lr_hierarchical_from_slash(path: str) -> str:
    return path.replace("/", "|")


def _strip_sola_keywords(items: set[str]) -> set[str]:
    out: set[str] = set()
    for s in items:
        if (
            s.startswith("sola/status/")
            or s.startswith("sola/reason/")
            or s.startswith("sola/usage/")
        ):
            continue
        if (
            s.startswith("sola|status|")
            or s.startswith("sola|reason|")
            or s.startswith("sola|usage|")
        ):
            continue
        out.add(s)
    return out


def _parse_xmp_inner(raw: str) -> ET.Element | None:
    """Strip XMP packet wrapper and return ``xmpmeta`` element, or None."""
    text = raw.strip()
    if not text:
        return None
    text = re.sub(r"<\?xpacket[^>]*\?>", "", text)
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    if root.tag == _XMPMETA:
        return root
    if root.tag.endswith("}xmpmeta") or root.tag == "xmpmeta":
        return root
    return None


def _parse_existing_label(desc: ET.Element) -> str | None:
    el = desc.find(Q(_NS_XMP, "Label"))
    if el is None or el.text is None:
        return None
    t = el.text.strip()
    return t or None


def _set_or_replace_child(parent: ET.Element, tag: str, text: str) -> None:
    el = parent.find(tag)
    if el is None:
        el = ET.SubElement(parent, tag)
    el.text = text


def _collect_bag_texts(bag: ET.Element) -> set[str]:
    return {li.text.strip() for li in bag.findall(_RDF_LI) if li.text}


def _rebuild_bag(parent: ET.Element, tag: str, values: list[str]) -> None:
    bag = ET.SubElement(parent, tag)
    for v in sorted(set(values)):
        li = ET.SubElement(bag, _RDF_LI)
        li.text = v


def _ensure_description(xmpmeta: ET.Element) -> ET.Element:
    rdf = xmpmeta.find(_RDF)
    if rdf is None:
        rdf = ET.SubElement(xmpmeta, _RDF)
    desc = rdf.find(_RDF_DESC)
    if desc is None:
        desc = ET.SubElement(rdf, _RDF_DESC)
        desc.set(Q(_NS_RDF, "about"), "")
    desc.set(f"{{{_NS_XMLNS}}}xmp", _NS_XMP)
    desc.set(f"{{{_NS_XMLNS}}}dc", _NS_DC)
    desc.set(f"{{{_NS_XMLNS}}}lr", _NS_LR)
    desc.set(f"{{{_NS_XMLNS}}}sola", _NS_SOLA)
    return desc


def build_xmp_packet(
    *,
    req: MetadataWriteRequest,
    color_label_lightroom: str | None,
    write_label: bool,
    overwrite_label: bool,
    write_keywords: bool,
    write_fields: bool,
    existing_xml: str | None,
) -> str:
    _register_namespaces()

    sola_status_kw = f"sola/status/{req.status}"
    sola_reason_kw = f"sola/reason/{normalize_gdpr_reason(req.reason)}"
    sola_usage_kw = f"sola/usage/{usage_keyword_token(req.usage)}"
    lr_status = _lr_hierarchical_from_slash(sola_status_kw)
    lr_reason = _lr_hierarchical_from_slash(sola_reason_kw)
    lr_usage = _lr_hierarchical_from_slash(sola_usage_kw)

    xmpmeta = _parse_xmp_inner(existing_xml) if existing_xml else None
    if xmpmeta is None:
        xmpmeta = ET.Element(_XMPMETA)
        xmpmeta.set(f"{{{_NS_XMLNS}}}x", _NS_X)
        _ensure_description(xmpmeta)

    desc = _ensure_description(xmpmeta)
    existing_label = _parse_existing_label(desc)

    if write_label and color_label_lightroom:
        if overwrite_label or not existing_label:
            _set_or_replace_child(desc, Q(_NS_XMP, "Label"), color_label_lightroom)

    if write_fields:
        _set_or_replace_child(desc, Q(_NS_SOLA, "gdpr_status"), req.status)
        _set_or_replace_child(desc, Q(_NS_SOLA, "gdpr_reason"), normalize_gdpr_reason(req.reason))
        _set_or_replace_child(desc, Q(_NS_SOLA, "gdpr_usage"), req.usage)
        if req.face_count is not None:
            _set_or_replace_child(desc, Q(_NS_SOLA, "faces_detected"), str(int(req.face_count)))
        if req.faces_identified is not None:
            _set_or_replace_child(desc, Q(_NS_SOLA, "faces_identified"), str(int(req.faces_identified)))
        if req.match_confidence_max is not None:
            _set_or_replace_child(
                desc, Q(_NS_SOLA, "match_confidence_max"), f"{req.match_confidence_max:.6g}"
            )

    if write_keywords:
        dc_tag = Q(_NS_DC, "subject")
        old = desc.find(dc_tag)
        dc_texts: set[str] = set()
        if old is not None:
            bag = old.find(_RDF_BAG)
            if bag is not None:
                dc_texts = _collect_bag_texts(bag)
            desc.remove(old)
        dc_texts = _strip_sola_keywords(dc_texts)
        dc_texts.add(sola_status_kw)
        dc_texts.add(sola_reason_kw)
        dc_texts.add(sola_usage_kw)
        subj = ET.SubElement(desc, dc_tag)
        _rebuild_bag(subj, _RDF_BAG, list(dc_texts))

        lr_tag = Q(_NS_LR, "hierarchicalSubject")
        old_lr = desc.find(lr_tag)
        lr_texts: set[str] = set()
        if old_lr is not None:
            bag = old_lr.find(_RDF_BAG)
            if bag is not None:
                lr_texts = _collect_bag_texts(bag)
            desc.remove(old_lr)
        lr_texts = _strip_sola_keywords(lr_texts)
        lr_texts.add(lr_status)
        lr_texts.add(lr_reason)
        lr_texts.add(lr_usage)
        h = ET.SubElement(desc, lr_tag)
        _rebuild_bag(h, _RDF_BAG, list(lr_texts))

    ET.indent(xmpmeta, space="  ")
    inner = ET.tostring(xmpmeta, encoding="unicode", xml_declaration=False)
    return (
        '<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
        f"{inner}\n"
        '<?xpacket end="w"?>\n'
    )


def write_sidecar(
    image_path: Path,
    req: MetadataWriteRequest,
    *,
    color_label_lightroom: str | None,
    write_label: bool,
    overwrite_label: bool,
    write_keywords: bool,
    write_fields: bool,
) -> None:
    path = Path(image_path)
    xmp_path = sidecar_path_for_image(path)
    existing: str | None = None
    if xmp_path.is_file():
        try:
            existing = xmp_path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            _LOG.warning("Could not read existing XMP %s: %s", xmp_path, e)

    packet = build_xmp_packet(
        req=req,
        color_label_lightroom=color_label_lightroom,
        write_label=write_label,
        overwrite_label=overwrite_label,
        write_keywords=write_keywords,
        write_fields=write_fields,
        existing_xml=existing,
    )
    xmp_path.write_text(packet, encoding="utf-8")
