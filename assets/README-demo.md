# Generative Workflow walkthrough

[generative-workflow-demo.gif](generative-workflow-demo.gif) is a **45-second, looping, illustrative animation** for the FlowX README. It shows the workflow layer behind an intelligent UI, not a shipped visual editor or a live recording. All tickets, results, node names, and timings are scripted examples; no model API or FlowX backend is called while rendering.

The design is a clean, black-and-white **Codex-style desktop workspace**: a slim left sidebar for projects and threads, a spacious central conversation, a rounded bottom composer with agent and permission controls, and a narrow right workflow inspector. Assistant responses use plain inline prose and lightweight action summaries instead of repeated avatars and role headers. The layout follows the project/thread organization described in [OpenAI's Codex app introduction](https://openai.com/index/introducing-the-codex-app/).

The existing host agent remains the speaker and uses FlowX as a workflow capability behind its own UI. There are no transport-specific labels: the concept works whether a host connects through an adapter, embeds the SDK, or supplies its own plugin integration. The Codex-style shell and custom result cards are an illustrative design, not a screenshot or a claim that Codex currently ships these cards or a FlowX integration.

Messages and **generative UI result cards** append to one continuous conversation, rather than replacing a fixed result panel. Each node adds an assistant message with its own card: ticket intake, category, priority, reply preview, and structured summary. Earlier messages stay in the stream while the viewport smoothly scrolls to the latest output. The user's refinement and next-ticket request continue that same thread.

## Storyboard

| Time | Moment | Idea illustrated |
| --- | --- | --- |
| 0–6s | Describe | Capture a support-ticket task in the host agent's interface. |
| 6–13s | Generate | Produce a workflow graph, Python node code, and a backend. |
| 13–20s | Inspect | Read the graph and discover step input formats. |
| 20–28s | Run | Append five assistant messages, each with a tailored node-result card, to the conversation. |
| 28–37s | Refine | Update `draft_reply` to include an escalation note for urgent tickets. |
| 37–45s | Reuse | Run five nodes on a new ticket; the reply card now includes an escalation note. |

The displayed workflow actions illustrate FlowX capabilities without prescribing a transport or API method. The workflow is illustrative: an actual graph, step IDs, and payloads depend on generation. A version badge shows the conceptual before/after refinement; it is not a built-in version-control feature. **Card rendering, card layouts, and the conversation interface belong to the conceptual host UI**, not a bundled FlowX frontend or an automatic UI-schema generation feature. The host-agent design is inspired by systems such as Codex; it does not imply a shipped Codex plugin or SDK-based execution API.

## Rebuild

The renderer uses **Node.js 20+**, **@resvg/resvg-js**, **FFmpeg**, and **Arial**. These are documentation-only tools, not FlowX runtime dependencies. To use another installed font, change `FONT` in [render-demo.mjs](render-demo.mjs).

With Node.js and FFmpeg available, install the SVG renderer outside the project and run from the repository root:

```bash
npm install --prefix /tmp/flowx-demo-tools --no-audit --no-fund @resvg/resvg-js@2.6.2
NODE_PATH=/tmp/flowx-demo-tools/node_modules node assets/render-demo.mjs
```

The script rasterizes SVG scenes into temporary PNG frames, encodes the GIF, and removes the temporary frames. It writes a 1152 × 736 animation at 4 fps, with an infinite loop and a 45-second cycle. The relatively low frame rate keeps the README asset lightweight and leaves time to read each stage.

To inspect six storyboard stills and all ten node-result cards (five for each run) without rebuilding the GIF:

```bash
NODE_PATH=/tmp/flowx-demo-tools/node_modules node assets/render-demo.mjs --preview-only --preview-dir /tmp/flowx-demo-preview
```

To render to another location and also save previews:

```bash
NODE_PATH=/tmp/flowx-demo-tools/node_modules node assets/render-demo.mjs --output /tmp/flowx-demo.gif --preview-dir /tmp/flowx-demo-preview
```

Check the encoded cycle duration rather than the illustrative on-screen timestamps:

```bash
ffprobe -v error -ignore_loop 1 -show_entries format=duration:stream=width,height,nb_frames -of json assets/generative-workflow-demo.gif
```

Keep the hero slogan, lifecycle, and example prompt aligned with the main [README](../README.md) when changing the storyboard. Keep the illustrative-demo label visible and do not imply a dedicated model integration or a bundled UI.