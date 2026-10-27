# Harness icon sources

Firstpane draws these images in Agents rows to identify which harness runs an agent
(`firstpane/icons.py`). Each file is a 64x64 8-bit RGBA PNG holding only IHDR, IDAT and IEND
chunks. Normalization cropped transparent margins and downscaled with an area filter. It did not
recolor, reshape, or change the aspect of any logo. The names and logos are trademarks of their
owners. Firstpane uses them only to say which tool runs an agent, and they imply no endorsement.

| File | Harness | Source | Normalization |
| --- | --- | --- | --- |
| `pi.png` | pi | `https://pi.dev/logo-auto.svg` (ETag `"83ce9b96de956e389a65386b733922a1"`, SHA-256 `abd66e78…cb4d6b`), the logo the pi package README shows. | Rasterized from the three flat SVG paths and cropped to the logo's bounds. |
| `omp.png` | omp | omp's primary logo: `packages/collab-web/public/favicon-512x512.png` in `github.com/can1357/oh-my-pi` at `0b5c27ab24719c86a5986b95ee55f4e75bc1aaa0` (SHA-256 `0e4b4837…cbdba`). It is the same image `https://omp.sh/favicon.png` serves: the mean difference at 64 px is 0.01/255. | Downscaled. |
| `claude.png` | Claude (Claude Code) | The Claude Code documentation favicon, `https://code.claude.com/docs/_mintlify/favicons/claude-code/pLsy-mRpNksna2sx/_generated/favicon-dark/android-chrome-192x192.png` (SHA-256 `d4e5e8e9…1a1707`). | Palette PNG to RGBA, then downscaled. |
| `grok.png` | Grok (Grok Build) | The Grok mark from `https://grok.com/images/android-chrome-512x512.png` (SHA-256 `3a462c3c…7e086`). The Grok Build docs use the xAI company logo, which names the provider, not the harness. | Downscaled. |
| `codex.png` | Codex (Codex CLI) | The icon of OpenAI's official VS Code extension "Codex – OpenAI's coding agent" (`openai.chatgpt` 26.51021.22027, verified publisher openai.com; its manifest names `resources/blossom.dark.png` as the icon): `https://openai.gallerycdn.vsassets.io/extensions/openai/chatgpt/26.51021.22027/1792788319200/Microsoft.VisualStudio.Services.Icons.Default` (SHA-256 `2ce1c57d…ee25d3`). OpenAI uses the Blossom as the Codex icon; no other Codex mark was reachable (openai.com/codex and chatgpt.com/codex refuse automated access, and the Codex desktop app is discontinued). | Downscaled. |

## Licenses

- **omp**: oh-my-pi is MIT (root `LICENSE`, Copyright (c) 2025 Mario Zechner, 2025-2026 Can
  Bölük, 2026 Stencil Labs, Inc.), and `@oh-my-pi/collab-web`, which ships the logo, declares
  `"license": "MIT"`. The full text is below.
- **pi**: the pi repository (`github.com/earendil-works/pi`) is MIT, Copyright (c) 2025 Mario
  Zechner, and its full text is below. The repository draws the same 4x4 logo in
  `packages/coding-agent/src/modes/interactive/components/pi-logo.ts`. The bundled colors come
  from the pi.dev SVG, and the site states no license for that file.
- **Claude**: Anthropic's Claude Code terms allow naming Claude Code in plain text. Any other use
  of its logos "requires our written permission" (https://code.claude.com/docs/en/legal-and-compliance,
  https://www.anthropic.com/legal/trademark-guidelines). No such permission is on record.
- **Grok**: xAI's Brand Guidelines allow using its marks only to accurately refer to Grok, and
  logos only "exactly as provided" in xAI's logo pack (https://x.ai/legal/brand-guidelines).
  Its consumer terms require xAI's permission for logo use. This file comes from the grok.com
  favicon, not that pack, and no permission is on record.
- **Codex**: OpenAI's Design Guidelines (https://openai.com/brand/) allow the logo only when it
  directly relates to OpenAI services, "exactly as provided". They also say not to "use the logo
  without permission or outside OpenAI's terms"; permission requests go to
  partnercomms@openai.com. No such permission is on record.
- Publishing the Claude, Grok and Codex images needs the permission described above. None is on
  record.

```
MIT License

Copyright (c) 2025 Mario Zechner
Copyright (c) 2025-2026 Can Bölük
Copyright (c) 2026 Stencil Labs, Inc.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
