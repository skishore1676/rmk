# SceneInfo profile qualified for PDF annotation reading

Version 0.2.1 decodes the optional firmware 3.27 SceneInfo fields left in
`extra_data` by rmscene 0.6.1. Wire definitions follow upstream rmscene PR #53,
head `0f1edc5e19ae45f2d6d0074e875a5270314424ce`:
https://github.com/ricklupton/rmscene/pull/53

Fields 5, 7 and 8 must agree with the supported 1404 × 1872 paper grid.
Field 6 is finite, positive-size viewport state; it is recorded and never
applied as a second ink transform. Field 9 is an undocumented byte, not a bool.
Only value 1 with fields 6, 7 and 8 present is qualified. Other values or
profiles, unknown fields, extra bytes, malformed lengths, duplicate fields,
nonfinite numbers and other paper grids fail before capture/checkpoint updates.

Qualification used the existing approved one-page PDF and its existing ink.
The source PDF bytes and annotation bytes were identical for both renderers.
An independent annotated PDF obtained through paired `rmapi geta` and the
candidate both rendered to 1786 × 2526 pixels at 3× PDF resolution. The ink
bounding boxes differed by one pixel; thresholded ink intersection-over-union
was 0.9224. Full-page visual inspection showed the original PDF background and
handwritten confirmation aligned, with the remaining page blank. This is an
independent renderer comparison, not an official tablet export or a general
claim about field 9 semantics. No personal ink or documents are stored in this
repository; regression fixtures are synthesized.

The pre-existing centered origin and 72/226 scale are retained, with no fitting
to ink bounds. Existing v1 PDF revisions remain readable after the v2 recipe
change. Reader acknowledgement still requires a complete successful read.

For the existing Air deployment, preserve the reviewed SSH command and remote
runtime. The account plugin requires the proven compatibility manifest with
`mcpServers: "./.mcp.json"`, no simultaneous root portable manifest and no
`${PLUGIN_ROOT}` working directory on the SSH launcher. Keep that deployment
overlay separate from this repository's generic local-stdio package. Verify
the installed plugin discovers all nine tools; direct SSH success alone is
not proof of installed-plugin integration.
