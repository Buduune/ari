"""Static checks on web/index.html: content from the brief, assets, and the contract with the backend."""

import re
from html.parser import HTMLParser

import pytest

import main
from conftest import ROOT

INDEX = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
SCRIPT = INDEX.split("<script>")[1].split("</script>")[0]


class _Collector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids, self.metas = [], []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if "id" in a:
            self.ids.append(a["id"])
        if tag == "meta":
            self.metas.append(a)


def parsed():
    c = _Collector()
    c.feed(INDEX)
    return c


def test_html_parses_cleanly():
    html5lib = pytest.importorskip("html5lib")
    parser = html5lib.HTMLParser(strict=False)
    parser.parse(INDEX)
    assert parser.errors == []


def test_ids_are_unique():
    ids = parsed().ids
    assert len(ids) == len(set(ids)), sorted(i for i in ids if ids.count(i) > 1)


def test_opts_out_of_forced_dark_mode():
    # Android Chrome / Samsung Internet / in-app browsers can auto-darken pages that don't say otherwise.
    metas = {m.get("name"): m.get("content") for m in parsed().metas}
    assert metas.get("color-scheme") == "only light"
    assert re.search(r"color-scheme:\s*only light", INDEX)


def _luminance(hex_color):
    rgb = [int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a, b):
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def css_var(name):
    return re.search(rf"--{name}:\s*(#[0-9a-fA-F]{{6}})", INDEX).group(1)


def test_text_colors_are_readable():
    paper, ink, graphite, sun, err = (css_var(n) for n in ("paper", "ink", "graphite", "sun", "err"))
    assert contrast(ink, paper) >= 7
    assert contrast(graphite, paper) >= 4.5      # door teasers, labels
    assert contrast(ink, sun) >= 4.5             # YES, selected chips
    assert contrast(err, paper) >= 4.5           # wrong square
    # Door 2's "Choose one" pill: white text on a blue gradient
    stops = re.search(r'\[data-world="bender"\] \.pill-main\{background:linear-gradient\(180deg,(#\w{6}),(#\w{6})\)', INDEX).groups()
    for stop in stops:
        assert contrast("#ffffff", stop) >= 4.5, stop
    # Door 1's pink pill with black text
    pink = re.search(r'\[data-world="stage"\] \.pill-main\{background:(#\w{6})', INDEX).group(1)
    assert contrast("#000000", pink) >= 4.5


def test_every_referenced_photo_exists():
    for path in set(re.findall(r"img/[\w-]+\.jpg", INDEX)):
        assert (ROOT / "web" / path).is_file(), path


def test_every_photo_has_a_full_credit():
    photos = re.findall(r"\{ src: 'img/[^}]+\}", SCRIPT)
    assert len(photos) == 12
    for p in photos:
        for field in ("cap:", "t:", "by:", "url: 'https://", "lic:", "licUrl: 'https://creativecommons.org/"):
            assert field in p, (field, p[:60])


def test_links_that_open_a_new_tab_are_safe():
    for m in re.finditer(r'target="_blank"', INDEX):
        assert 'rel="noopener' in INDEX[m.end():m.end() + 30]


def test_copy_from_the_brief():
    for text in [
        "Hey, nice to see you again",
        "<span>Would you</span><span>meet me?</span>",
        "Pick a door.", "Each one’s a different kind of day.",
        "title: 'The Stage', teaser: 'Music'",
        "title: 'The Mind-Bender', teaser: 'Now you see me. No'",
        "title: 'Academic', teaser: 'Poetry autumn'",
        "Museum of Pop Culture (MoPOP) at Seattle Center. We wander the music and sci-fi exhibits, then head to the Sound Lab where we grab instruments, hit the booths. Encouraged, even. Afterward, a walk around Seattle Center with the Space Needle lit up above us.",
        "Museum of Illusions. Upside-down rooms, shrinking rooms, and way too many photos of us looking ridiculous. Then we warm up with coffee at Starbucks near Pike Place, sit by the window, and compare who fell for more illusions.",
        "University of Washington campus tour, talking about Russian literature, Japanese haiku, writing poems together.",
        "href: 'https://www.mopop.org/'", "href: 'https://moiseattle.com/'",
        "Afternoon", "Golden hour", "Evening",
        "Sunset’s the whole point of this one 🌅",
        "Anything you want me to know?", "(food, allergies, what to wear…)",
        "Send it", "My pleasure.", "ok fine… yes 🙈", "Choose one",
    ]:
        assert text in INDEX, text
    assert "💛" not in INDEX  # removed on request


def test_page_sends_exactly_the_fields_the_backend_expects():
    block = SCRIPT.split("const payload = {")[1].split("};")[0]
    sent = set(re.findall(r"(?:^|[\s,{])([A-Za-z]+):\s", block))
    assert sent == set(main.Reply.model_fields), sent ^ set(main.Reply.model_fields)


def test_note_limit_is_the_same_on_both_sides():
    limit = next(m.max_length for m in main.Reply.model_fields["note"].metadata if hasattr(m, "max_length"))
    assert f'maxlength="{limit}"' in INDEX
    assert f".slice(0, {limit})" in SCRIPT
