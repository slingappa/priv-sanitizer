# Privacy policy

The baseline pass must work with a video only. It uses conservative visual blurring, local layout analysis, and transcript-free speech activity detection as evidence. Audio is preserved by default, with an explicit unresolved-risk warning for spoken participant names.

The optional transcript is a secondary hardening input. It can add names, speaker labels, and timing evidence, but it never becomes a required input and is never rewritten as an output. Transcript-based audio muting is experimental and off by default; it requires explicit opt-in.

Blur is not assumed to be irreversible. The renderer therefore records confidence and review requirements, and uses conservative participant-panel coverage when OCR/face backends are unavailable.
