# terminal-browser 0.13.4 tooltip context

Firstpane can preview a select-tool note without a second editor only when the browser posts that note as `context` beside the element `text`. The exact substitutions are in `terminal-browser-0.13.4-context-substitutions.json`. Apply them to a copy of unpacked 0.13.4 with `apply-terminal-browser-0.13.4-context.py`. Landing this repository does not apply that patch and does not change `terminal_browser_root`. The installed browser and the configured 0.13.4 tree stay as they are until someone applies the patch there.

Configured runtime checked for this work: `terminal-browser v0.13.4`.

Original file hashes:

- `browser/dist/main.js` `cf73503a06a944da8e349d4e60a7bb221e028255b02851bddecd7a65ba6e7175`
- `assets/react-grab/index.global.js` `effbc12a5d13e94664c1a20adb4cf90b477487a06571421c5ab3a64f09439f98`

The JSON file is the patch. Each `old` string occurs once in the named 0.13.4 file; replace it with `new`. Do not rewrite `text` to include the note. Applying those pairs to the tested 0.13.4 sender files reproduces the patched sender byte for byte.

In `browser/dist/main.js`:

- `emit({ type: "selected", content });` becomes `emit({ type: "selected", content, context: typeof note === "string" ? note : "" });` and the hook gains a `note` parameter.
- `this.hooks.selected(message.content);` becomes `this.hooks.selected(message.content, message.context);`
- `sendGrab` and the embedded agent `send` take that second value and post `{"text": content, "context": note}` when the note is a non-empty string. Otherwise the body stays `{"text": content}`.

In `assets/react-grab/index.global.js`:

- `transformCopyContent:async(e,t)=>w(\`transformCopyContent\`,e,t)` becomes `transformCopyContent:async(e,t,r)=>w(\`transformCopyContent\`,e,t,r)`
- `t.transformCopyContent(c,n)` becomes `t.transformCopyContent(c,n,r)`

`r` is the typed tooltip note. `c` stays the element content. The clipboard string is still built after the hook, so the note is not copied into `text`.

A live tooltip click was not run. The proof executed these sender functions from the 0.13.4 bundle against Firstpane's agent bridge: the unpatched sender dropped the note, and the patched sender delivered it once as `context`.
