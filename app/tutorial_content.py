import html as _html
import json
import re

from flask import current_app

from .models import TutorialPageConfig
from . import db


TUTORIAL_CONFIG_SLUG = 'main'
TUTORIAL_SCHEMA_VERSION = 1


_RE_SECTION = re.compile(
    r'(?P<full><section\s+id="(?P<id>[^"]+)"\s+class="tutorial-section"[^>]*>)(?P<body>[\s\S]*?)</section>',
    re.DOTALL,
)


def _strip_tags(s: str) -> str:
    s = _html.unescape(s or '')
    # Hilangkan tag HTML yang muncul di dalam `<strong>...</strong>` dll.
    s = re.sub(r'<[^>]+>', '', s)
    # Normalisasi whitespace
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def _extract_first(pattern: re.Pattern, text: str, default: str = '') -> str:
    m = pattern.search(text or '')
    if not m:
        return default
    return _strip_tags(m.group(1))


def _extract_all(pattern: re.Pattern, text: str):
    out = []
    for m in pattern.finditer(text or ''):
        out.append(_strip_tags(m.group(1)))
    return out


def _extract_texts_in_order(text: str):
    """
    Ambil potongan teks dari urutan kemunculan untuk jadi `bullets[]`.
    Tujuan utama: menangkap `li` dan `span.text-ink-muted` yang sering dipakai di template tutorial.
    """
    candidates = []

    # 1) <li>...</li>
    for m in re.finditer(r'<li[^>]*>([\s\S]*?)</li>', text or '', flags=re.DOTALL):
        candidates.append((m.start(), _strip_tags(m.group(1))))

    # 2) <span class="text-ink-muted">...</span>
    for m in re.finditer(
        r'<span[^>]*class="[^"]*text-ink-muted[^"]*"[^>]*>([\s\S]*?)</span>',
        text or '',
        flags=re.DOTALL,
    ):
        candidates.append((m.start(), _strip_tags(m.group(1))))

    # 3) fallback: p.text-ink-muted
    for m in re.finditer(
        r'<p[^>]*class="[^"]*text-ink-muted[^"]*"[^>]*>([\s\S]*?)</p>',
        text or '',
        flags=re.DOTALL,
    ):
        candidates.append((m.start(), _strip_tags(m.group(1))))

    # Sort by appearance and unique consecutive
    candidates.sort(key=lambda x: x[0])
    seen = set()
    out = []
    for _, t in candidates:
        if not t:
            continue
        t_norm = t.strip()
        if not t_norm:
            continue
        # Hindari duplikasi persis yang sering terjadi karena markup mirip.
        if t_norm in seen:
            continue
        seen.add(t_norm)
        out.append(t_norm)

    return out


def _extract_card_by_label(label: str, section_html: str):
    """
    Extract kartu WHAT/WHY/... dari bagian 5W1H.
    Mengandalkan struktur template saat ini.
    """
    # icon emoji ada di div berkelas "w-11 h-11 ...">ICON</div>
    # judul ada di p.text-sm.font-semibold...
    # deskripsi ada di p.text-xs...
    card_pattern = re.compile(
        rf'<!--\s*{re.escape(label)}\s*-->([\s\S]*?)'
        r'<!--\s*(?:WHAT|WHY|WHO|WHEN|WHERE|HOW)\s*-->',
        re.DOTALL,
    )
    m = card_pattern.search(section_html)
    if not m:
        # Last card ends before grid closes.
        if label == 'HOW':
            card_html = section_html
        else:
            return None
    else:
        card_html = m.group(1)

    # label "WHAT/WHY" (upper already)
    icon = _extract_first(
        re.compile(r'<div[^>]*w-11[^>]*h-11[^>]*>\s*([\s\S]*?)\s*</div>', re.DOTALL),
        card_html,
        default='',
    )
    # judul
    title = _extract_first(
        re.compile(r'<p[^>]*class="[^"]*text-sm[^"]*font-semibold[^"]*"[^>]*>([\s\S]*?)</p>', re.DOTALL),
        card_html,
        default='',
    )
    # deskripsi
    description = _extract_first(
        re.compile(r'<p[^>]*class="[^"]*text-xs[^"]*text-ink-muted[^"]*[^>]*>([\s\S]*?)</p>', re.DOTALL),
        card_html,
        default='',
    )
    # subtitle: ada span.text-xs.font-semibold.text-ink-muted pada header
    subtitle = _extract_first(
        re.compile(
            r'<span[^>]*class="[^"]*text-xs[^"]*font-semibold[^"]*text-ink-muted[^"]*"[^>]*>([\s\S]*?)</span>',
            re.DOTALL,
        ),
        card_html,
        default='',
    )

    # Bullets opsional: pada template 5W1H, umumnya tidak ada list.
    bullets = []
    if description:
        bullets = [description]

    return {
        'label': label,
        'icon': icon,
        'subtitle': subtitle,
        'title': title,
        'description': description,
        'bullets': bullets,
    }


def extract_default_tutorial_data_from_template(template_html: str):
    """
    Build default structured tutorial data dari template `tutorial.html`.

    Catatan: ekstraksi ini berbasis regex (tanpa HTML parser), jadi jika template berubah besar,
    seeding mungkin perlu penyesuaian.
    """
    text = template_html or ''

    def _between(start_marker: str, end_marker: str) -> str:
        pattern = re.compile(
            re.escape(start_marker) + r'\s*(?P<block>[\s\S]*?)\s*' + re.escape(end_marker),
            re.DOTALL,
        )
        m = pattern.search(text)
        return m.group('block') if m else ''

    hero_html = _between('<!-- ── HERO TUTORIAL ── -->', '<!-- ── PENGENALAN 5W1H ── -->')
    fivew1h_html = _between('<!-- ── PENGENALAN 5W1H ── -->', '<!-- ── TABLE OF CONTENTS CARDS ── -->')
    toc_html = _between('<!-- ── TABLE OF CONTENTS CARDS ── -->', '<!-- ── MAIN CONTENT + SIDEBAR ── -->')
    sidebar_html = _between('<!-- Sidebar (desktop) -->', '<!-- Tutorial content -->')

    # HERO
    hero_badge = _extract_first(
        re.compile(r'<span[^>]*chip[^>]*>\s*([\s\S]*?)\s*</span>', re.DOTALL),
        hero_html,
        default='',
    )
    hero_heading = _extract_first(
        re.compile(r'<h1[^>]*>([\s\S]*?)</h1>', re.DOTALL),
        hero_html,
        default='',
    )
    hero_lead = _extract_first(
        re.compile(r'<p[^>]*class="[^"]*text-lg[^"]*text-ink-muted[^"]*"[^>]*>([\s\S]*?)</p>', re.DOTALL),
        hero_html,
        default='',
    )

    cta_primary = None
    cta_secondary = None
    # ambil dua anchor CTA di hero: '#mulai' dan '#pos'
    for href, target_id in [('#mulai', 'mulai'), ('#pos', 'pos')]:
        link_pat = re.compile(
            rf'<a[^>]*href="{re.escape(href)}"[^>]*>([\s\S]*?)</a>',
            re.DOTALL,
        )
        a_text = _extract_first(link_pat, hero_html, default='')
        if target_id == 'mulai':
            cta_primary = {'href': f'#{target_id}', 'text': a_text}
        elif target_id == 'pos':
            cta_secondary = {'href': f'#{target_id}', 'text': a_text}

    stats = []
    for m in re.finditer(
        r'<div class="text-center"><div class="text-2xl font-bold text-brand">\s*([\s\S]*?)\s*</div><div class="text-xs text-ink-muted mt-0\.5">\s*([\s\S]*?)\s*</div></div>',
        hero_html,
        re.DOTALL,
    ):
        stats.append({'value': _strip_tags(m.group(1)), 'label': _strip_tags(m.group(2))})

    # 5W1H cards
    five_cards = []
    for label in ['WHAT', 'WHY', 'WHO', 'WHEN', 'WHERE', 'HOW']:
        card = _extract_card_by_label(label, fivew1h_html)
        if card:
            five_cards.append(card)

    # TOC cards + ordering
    toc_cards = []
    for m in re.finditer(r'<a\s+href="#([^"]+)"[^>]*>[\s\S]*?</a>', toc_html, flags=re.DOTALL):
        anchor_block = m.group(0)
        sid = m.group(1)
        icon = _extract_first(
            re.compile(r'<span[^>]*text-2xl[^>]*>\s*([\s\S]*?)\s*</span>', re.DOTALL),
            anchor_block,
            default='',
        )
        title = _extract_first(
            re.compile(r'text-sm font-semibold[^>]*>\s*([\s\S]*?)\s*</div>', re.DOTALL),
            anchor_block,
            default='',
        )
        subtitle = _extract_first(
            re.compile(r'text-xs[^>]*text-ink-faint[^>]*>\s*([\s\S]*?)\s*</div>', re.DOTALL),
            anchor_block,
            default='',
        )
        toc_cards.append({
            'id': sid,
            'icon': icon,
            'title': title,
            'subtitle': subtitle,
        })

    sections_order = [c['id'] for c in toc_cards if c.get('id')]

    # Sidebar links
    sidebar_links = []
    for m in re.finditer(r'<a\s+href="#([^"]+)"[^>]*class="[^"]*sidebar-link[^"]*"[^>]*>([\s\S]*?)</a>', sidebar_html, flags=re.DOTALL):
        sid = m.group(1)
        inner = _strip_tags(m.group(2))
        # inner format: "🚀 Memulai" etc.
        parts = inner.split(' ', 1)
        icon = parts[0].strip() if parts else ''
        title = parts[1].strip() if len(parts) > 1 else inner
        sidebar_links.append({'id': sid, 'icon': icon, 'title': title})

    # Sections + steps
    sections = []
    for m in _RE_SECTION.finditer(text):
        sid = m.group('id')
        section_html = m.group('full') + m.group('body') + '</section>'

        section_title = _extract_first(
            re.compile(r'<h2[^>]*>([\s\S]*?)</h2>', re.DOTALL),
            section_html,
            default='',
        )
        section_subtitle = _extract_first(
            re.compile(r'<p[^>]*class="[^"]*text-sm[^"]*text-ink-muted[^"]*"[^>]*>([\s\S]*?)</p>', re.DOTALL),
            section_html,
            default='',
        )
        # header badge: w-10 h-10 ...</div> near section title block
        badge_text = _extract_first(
            re.compile(r'<div[^>]*w-10[^>]*h-10[^>]*>\s*([\s\S]*?)\s*</div>', re.DOTALL),
            section_html,
            default='',
        )

        # Parse steps inside section
        step_chip_re = re.compile(
            r'<span[^>]*class="[^"]*chip[^"]*"[^>]*>\s*(Langkah|Step)\s+(\d+)\s*</span>\s*<h3[^>]*>\s*([\s\S]*?)\s*</h3>',
            re.DOTALL,
        )
        chip_matches = list(step_chip_re.finditer(section_html))
        steps = []

        for idx, cm in enumerate(chip_matches):
            chip_prefix = cm.group(1)
            step_number = int(_strip_tags(cm.group(2)))
            step_title = _strip_tags(cm.group(3))

            # Body slice until next chip match start (to keep content inside the step card)
            body_start = cm.end()
            body_end = chip_matches[idx + 1].start() if idx + 1 < len(chip_matches) else len(section_html)
            body_slice = section_html[body_start:body_end]

            # lead = first muted paragraph if ada
            lead = _extract_first(
                re.compile(r'<p[^>]*class="[^"]*text-sm[^"]*text-ink-muted[^"]*"[^>]*>([\s\S]*?)</p>', re.DOTALL),
                body_slice,
                default='',
            )

            # bullets: capture li / spans in order; remove lead if repeated
            bullets = _extract_texts_in_order(body_slice)
            if lead:
                bullets = [b for b in bullets if b != lead]

            steps.append({
                'step_number': step_number,
                'chip_prefix': chip_prefix,
                'title': step_title,
                'lead': lead,
                'bullets': bullets,
            })

        sections.append({
            'id': sid,
            'header_badge': badge_text,
            'title': section_title,
            'subtitle': section_subtitle,
            'steps': steps,
        })

    # Sort sections by toc order first; fallback to discovery order
    section_by_id = {s['id']: s for s in sections}
    ordered_sections = [section_by_id[sid] for sid in sections_order if sid in section_by_id]
    # add missing (jika toc belum lengkap)
    for sid in section_by_id:
        if sid not in sections_order:
            ordered_sections.append(section_by_id[sid])

    return {
        'schema_version': TUTORIAL_SCHEMA_VERSION,
        'hero': {
            'badge': hero_badge,
            'heading': hero_heading,
            'lead': hero_lead,
            'cta_primary': cta_primary,
            'cta_secondary': cta_secondary,
            'stats': stats,
        },
        'fiveW1H': {
            'cards': five_cards,
        },
        'toc': {
            'cards': toc_cards,
        },
        'sidebar': {
            'links': sidebar_links,
        },
        'sections_order': sections_order,
        'sections': ordered_sections,
    }


def build_default_tutorial_data():
    # Jinja loader get_source -> (source, filename, uptodate)
    source, _, _ = current_app.jinja_loader.get_source(current_app, 'tutorial.html')
    return extract_default_tutorial_data_from_template(source)


def ensure_tutorial_page_config_default(slug: str = TUTORIAL_CONFIG_SLUG, aktif: bool = True):
    """
    Pastikan ada record `TutorialPageConfig` untuk tutorial utama.
    Jika belum ada, seed dari template `tutorial.html`.
    """
    cfg = TutorialPageConfig.query.filter_by(slug=slug).first()
    if cfg and cfg.aktif == aktif and cfg.data_json:
        try:
            data = json.loads(cfg.data_json)
            data = normalize_tutorial_data(data)
            errors = validate_tutorial_data_structure(data)
            if not errors:
                cfg.data_json = json.dumps(data, ensure_ascii=False)
                db.session.commit()
                return cfg
        except Exception:
            pass

    # Pakai schema_version baru
    data = build_default_tutorial_data()
    data = normalize_tutorial_data(data)
    errors = validate_tutorial_data_structure(data)
    if errors:
        raise ValueError('Seed tutorial invalid: ' + '; '.join(errors[:10]))

    # Sertakan seed state dalam session cache agar tidak dobel create
    if cfg is None:
        cfg = TutorialPageConfig(slug=slug, schema_version=TUTORIAL_SCHEMA_VERSION, aktif=aktif)
        db.session.add(cfg)

    cfg.schema_version = TUTORIAL_SCHEMA_VERSION
    cfg.aktif = aktif
    cfg.data_json = json.dumps(data, ensure_ascii=False)
    # updated_by bisa None saat seed startup
    db.session.commit()
    return cfg


def normalize_tutorial_data(data):
    """Normalisasi ringan agar struktur aman untuk renderer/editor."""
    if not isinstance(data, dict):
        return {}

    out = dict(data)
    out['hero'] = out.get('hero') if isinstance(out.get('hero'), dict) else {}
    out['fiveW1H'] = out.get('fiveW1H') if isinstance(out.get('fiveW1H'), dict) else {}
    out['toc'] = out.get('toc') if isinstance(out.get('toc'), dict) else {}
    out['sidebar'] = out.get('sidebar') if isinstance(out.get('sidebar'), dict) else {}

    if not isinstance(out['fiveW1H'].get('cards'), list):
        out['fiveW1H']['cards'] = []
    if not isinstance(out['toc'].get('cards'), list):
        out['toc']['cards'] = []
    if not isinstance(out['sidebar'].get('links'), list):
        out['sidebar']['links'] = []
    if not isinstance(out.get('sections'), list):
        out['sections'] = []
    if not isinstance(out.get('sections_order'), list):
        out['sections_order'] = []

    # Normalize hero
    hero = out['hero']
    for k in ['badge', 'heading', 'lead']:
        if hero.get(k) is None:
            hero[k] = ''
        if not isinstance(hero.get(k), str):
            hero[k] = str(hero.get(k) or '')
    for k in ['cta_primary', 'cta_secondary']:
        v = hero.get(k)
        if not isinstance(v, dict):
            hero[k] = {'href': '', 'text': ''}
        else:
            hero[k].setdefault('href', '')
            hero[k].setdefault('text', '')

    # Normalize fiveW1H cards
    cards = out['fiveW1H']['cards']
    for c in cards:
        if not isinstance(c, dict):
            continue
        c.setdefault('label', '')
        c.setdefault('icon', '')
        c.setdefault('subtitle', '')
        c.setdefault('title', '')
        c.setdefault('description', '')
        if c.get('bullets') is None:
            c['bullets'] = []
        if isinstance(c.get('bullets'), str):
            c['bullets'] = [c['bullets']]
        if not isinstance(c.get('bullets'), list):
            c['bullets'] = []

    # Normalize toc cards
    toc_cards = out['toc']['cards']
    for c in toc_cards:
        if not isinstance(c, dict):
            continue
        c.setdefault('id', '')
        c.setdefault('icon', '')
        c.setdefault('title', '')
        c.setdefault('subtitle', '')

    # Normalize sidebar links
    for l in out['sidebar']['links']:
        if not isinstance(l, dict):
            continue
        l.setdefault('id', '')
        l.setdefault('icon', '')
        l.setdefault('title', '')

    # Normalize sections and steps
    for sec in out['sections']:
        if not isinstance(sec, dict):
            continue
        sec.setdefault('id', '')
        sec.setdefault('header_badge', '')
        sec.setdefault('title', '')
        sec.setdefault('subtitle', '')
        if not isinstance(sec.get('steps'), list):
            sec['steps'] = []
        for st in sec['steps']:
            if not isinstance(st, dict):
                continue
            st.setdefault('step_number', 1)
            st.setdefault('chip_prefix', 'Langkah')
            st.setdefault('title', '')
            st.setdefault('lead', '')
            if st.get('bullets') is None:
                st['bullets'] = []
            if isinstance(st.get('bullets'), str):
                st['bullets'] = [st['bullets']]
            if not isinstance(st.get('bullets'), list):
                st['bullets'] = []

    return out


def validate_tutorial_data_structure(data):
    """
    Validasi struktur minimum agar renderer/editor tidak crash.

    Return:
      errors: list[str]
    """
    errors = []

    if not isinstance(data, dict):
        return ['data is not dict']

    if not isinstance(data.get('schema_version'), int):
        errors.append('schema_version must be int')

    hero = data.get('hero')
    if not isinstance(hero, dict):
        errors.append('hero must be dict')
    else:
        for k in ['badge', 'heading', 'lead']:
            if k not in hero:
                errors.append(f'hero.{k} missing')

    five = data.get('fiveW1H')
    if not isinstance(five, dict) or not isinstance(five.get('cards'), list):
        errors.append('fiveW1H.cards must be list')
    else:
        if len(five.get('cards')) < 6:
            errors.append('fiveW1H.cards must have at least 6 cards')
        for i, c in enumerate(five.get('cards')[:6]):
            if not isinstance(c, dict):
                errors.append(f'fiveW1H.cards[{i}] must be dict')

    toc = data.get('toc')
    if not isinstance(toc, dict) or not isinstance(toc.get('cards'), list):
        errors.append('toc.cards must be list')

    sidebar = data.get('sidebar')
    if not isinstance(sidebar, dict) or not isinstance(sidebar.get('links'), list):
        errors.append('sidebar.links must be list')

    sections = data.get('sections')
    if not isinstance(sections, list) or not sections:
        errors.append('sections must be non-empty list')
    else:
        for sidx, sec in enumerate(sections):
            if not isinstance(sec, dict):
                errors.append(f'sections[{sidx}] not dict')
                continue
            if not sec.get('id'):
                errors.append(f'sections[{sidx}].id missing')
            if not isinstance(sec.get('steps'), list):
                errors.append(f'sections[{sidx}].steps must be list')
                continue
            for stidx, st in enumerate(sec.get('steps')[:50]):
                if not isinstance(st, dict):
                    errors.append(f'sections[{sidx}].steps[{stidx}] not dict')
                    continue
                if 'step_number' not in st:
                    errors.append(f'sections[{sidx}].steps[{stidx}].step_number missing')
                if not isinstance(st.get('bullets'), list):
                    errors.append(f'sections[{sidx}].steps[{stidx}].bullets must be list')

    return errors

