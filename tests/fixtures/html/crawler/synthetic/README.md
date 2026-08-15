# Synthetic fixtures

Every other fixture under `tests/fixtures/html/crawler/` is a real page,
fetched live and saved verbatim (see the provenance comment at the top of
each file). These four are not.

After a genuinely diligent search — multiple targeted queries, direct
fetch attempts against universities, nonprofits, a legal aid clinic, and a
blog post specifically about email obfuscation — no live page could be
found among larger/public organizations still using classic bracket-style
obfuscation (`name [at] domain [dot] com`) or an HTML-entity-encoded
`mailto:`. Modern larger sites have largely moved to contact forms or
JS-based cloaking instead. Two other patterns (a credentialed-name byline
and a customer-testimonial attribution) are also synthetic, chosen
deliberately here rather than sourced from a small individual medical
practice or review site, which would have meant a real, non-consenting
person's name in this repo for no better reason than convenience.

These four files are hand-written, using realistic conventions, purely to
exercise the parser's handling of well-known patterns:

- `obfuscated_email_bracket_at.html` — `name [at] domain [dot] com`
- `obfuscated_email_html_entity.html` — `&#106;&#111;...`-encoded mailto
- `credentialed_name.html` — `Jane Smith, DDS` byline style
- `testimonial_and_section_heading_noise.html` — the two classic
  person-extraction false positives: a testimonial author, and a bare
  "Our Team" section heading with no name attached
