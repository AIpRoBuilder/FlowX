/**
 * Render the README's illustrative walkthrough, without running FlowX or an LLM.
 * Requires Node.js 20+, @resvg/resvg-js, FFmpeg, and the Arial font.
 */
import { spawnSync } from "node:child_process";
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
let Resvg;
const WIDTH = 1152;
const HEIGHT = 736;
const FPS = 4;
const DURATION = 45;
const FONT = "Arial";
const ASSETS = dirname(fileURLToPath(import.meta.url));
const COLORS = {
  text: "#171717",
  muted: "#737373",
  faint: "#a3a3a3",
  line: "#e5e5e5",
  panel: "#ffffff",
  soft: "#f5f5f5",
  background: "#fafafa",
};
const STEP_SECONDS = 1.35;
const NAV = { x: 28, width: 162 };
const CHAT = { x: 190, width: 714, contentX: 226, contentWidth: 642 };
const SIDEBAR = { x: 904, width: 220 };
const STREAM = { top: 178, height: 341, userHeight: 92, assistantHeight: 262, enterSeconds: 0.6 };

const SCENES = [
  {
    start: 0, end: 6, label: "Describe",
    title: "Start with intent. Not a diagram.",
    subtitle: "An agent captures the task in the interface you already use.",
    prompt: ["Build a support-ticket workflow that classifies tickets,", "sets priority, and drafts a reply."],
    action: "FlowX · Understanding the workflow request",
    reply: "I'll turn this task into a workflow you can run and refine.",
  },
  {
    start: 6, end: 13, label: "Generate",
    title: "Generate the process behind the UI.",
    subtitle: "FlowX turns the request into a graph, node code, and a runnable backend.",
    prompt: ["Build a support-ticket workflow that classifies tickets,", "sets priority, and drafts a reply."],
    action: "FlowX · Generating the workflow",
    reply: "The process now exists beyond the conversation.",
  },
  {
    start: 13, end: 20, label: "Inspect",
    title: "Make the workflow inspectable.",
    subtitle: "The agent can read the graph, discover input formats, and inspect source files.",
    prompt: ["Show me the workflow and the inputs each step expects."],
    action: "FlowX · Inspecting the graph and step inputs",
    reply: "Five explicit nodes. Defined inputs. Editable source files.",
  },
  {
    start: 20, end: 28, label: "Run",
    title: "Node results, in your agent's conversation.",
    subtitle: "Your agent uses FlowX; result cards arrive in the same conversation stream.",
    prompt: ["Run this ticket: Billing is broken. Our whole team is blocked."],
  },
  {
    start: 28, end: 37, label: "Refine",
    title: "Change the intent. Evolve the workflow.",
    subtitle: "A follow-up request updates the reply node, rather than starting over.",
    prompt: ["For urgent tickets, include an escalation note in the reply."],
    action: "FlowX · Refining the reply node",
    reply: "The reply logic is updated. The other nodes stay unchanged.",
  },
  {
    start: 37, end: 45, label: "Reuse",
    title: "One conversation. A reusable process.",
    subtitle: "The conversation continues: new input, the same workflow, and updated result cards.",
    prompt: ["Reload and run the next ticket: Our billing page is down."],
  },
];

const NODES = [
  { label: "Ticket input", kind: "Support ticket", id: "input", short: "Input", reply: "Ticket received. Here's the input the workflow will use." },
  { label: "Classify", kind: "Category", id: "classify", short: "Category", reply: "This is a billing issue. Here's the classification card." },
  { label: "Prioritize", kind: "Urgency", id: "prioritize", short: "Priority", reply: "The team is blocked. This ticket needs urgent attention." },
  { label: "Draft reply", kind: "Response", id: "draft_reply", short: "Reply", reply: "A reply is ready to review, not just a line of tool output." },
  { label: "Structured output", kind: "Result", id: "output", short: "Result", reply: "The run is complete. Here's the reusable result card." },
];

function xml(value) {
  return String(value).replace(/[<>&"']/g, (character) => ({
    "<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;", "'": "&apos;",
  })[character]);
}

function text(x, y, value, size = 16, color = COLORS.text, weight = 400) {
  return `<text x="${x}" y="${y}" font-family="${FONT}" font-size="${size}" font-weight="${weight}" fill="${color}">${xml(value)}</text>`;
}

function lines(x, y, values, size = 16, color = COLORS.text, spacing = 25) {
  return values.map((value, index) => text(x, y + index * spacing, value, size, color)).join("");
}

function rect(x, y, width, height, fill, stroke = "none", radius = 12) {
  return `<rect x="${x}" y="${y}" width="${width}" height="${height}" rx="${radius}" fill="${fill}" stroke="${stroke}"/>`;
}

function pill(x, y, width, value, filled = false) {
  return rect(x, y, width, 24, filled ? COLORS.text : COLORS.soft, "none", 6)
    + text(x + 10, y + 16, value, 11, filled ? COLORS.panel : COLORS.muted, 700);
}

function rule(x1, y1, x2, y2, color = COLORS.line) {
  return `<path d="M ${x1} ${y1} L ${x2} ${y2}" stroke="${color}" fill="none"/>`;
}

function check(x, y, color = COLORS.text) {
  return `<path d="M ${x - 4} ${y} L ${x - 1} ${y + 3} L ${x + 5} ${y - 4}" stroke="${color}" stroke-width="1.7" fill="none" stroke-linecap="round" stroke-linejoin="round"/>`;
}

function icon(x, y, name, color = COLORS.muted) {
  const paths = {
    edit: '<path d="M 3 6 V 14 H 13 V 10 M 7 10 L 8 6 L 13 1 L 16 4 L 11 9 Z"/>',
    clock: '<circle cx="8" cy="8" r="6"/><path d="M 8 4 V 8 L 11 10"/>',
    grid: '<rect x="2" y="2" width="5" height="5" rx="1"/><rect x="10" y="2" width="5" height="5" rx="1"/><rect x="2" y="10" width="5" height="5" rx="1"/><rect x="10" y="10" width="5" height="5" rx="1"/>',
    folder: '<path d="M 2 4 H 7 L 9 6 H 15 V 14 H 2 Z"/>',
    branch: '<circle cx="4" cy="3" r="2"/><circle cx="4" cy="13" r="2"/><circle cx="13" cy="3" r="2"/><path d="M 4 5 V 11 M 13 5 Q 13 9 4 9"/>',
    settings: '<circle cx="8" cy="8" r="3"/><path d="M 8 1 V 3 M 8 13 V 15 M 1 8 H 3 M 13 8 H 15 M 3 3 L 4 4 M 12 12 L 13 13 M 3 13 L 4 12 M 12 4 L 13 3"/>',
    panel: '<rect x="1" y="2" width="14" height="12" rx="2"/><path d="M 10 2 V 14"/>',
  };
  return `<g transform="translate(${x} ${y})" fill="none" stroke="${color}" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round">${paths[name] || paths.panel}</g>`;
}

function navigation() {
  return [
    rect(NAV.x + 1, 59, NAV.width - 1, 596, "#f7f7f7", "none", 0),
    ...[49, 65, 81].map((x) => `<circle cx="${x}" cy="77" r="4.5" fill="#d4d4d4"/>`),
    text(47, 113, "Codex", 20, COLORS.text, 700),
    icon(161, 99, "panel"),
    icon(46, 138, "edit", COLORS.text),
    text(73, 151, "New thread", 13),
    icon(46, 170, "clock"),
    text(73, 183, "Automations", 13, COLORS.muted),
    icon(46, 202, "grid"),
    text(73, 215, "Skills", 13, COLORS.muted),
    text(46, 256, "Projects", 11, COLORS.muted, 700),
    text(167, 256, "+", 16, COLORS.muted),
    icon(46, 273, "folder"),
    text(73, 286, "FlowX", 13, COLORS.text, 700),
    rect(39, 299, 141, 30, "#e9e9e9", "none", 6),
    text(50, 318, "Support ticket triage", 11, COLORS.text, 700),
    text(51, 351, "Review workflow", 11, COLORS.muted),
    text(51, 383, "Explore agent tools", 11, COLORS.muted),
    text(46, 437, "Threads stay in one project", 10, COLORS.faint),
    icon(46, 600, "settings"),
    text(73, 613, "Settings", 12, COLORS.muted),
    `<circle cx="54" cy="640" r="10" fill="#e5e5e5"/>`,
    text(50, 644, "P", 10, COLORS.muted, 700),
    text(73, 644, "Workspace", 12, COLORS.muted),
    rule(CHAT.x, 59, CHAT.x, 656),
  ].join("");
}

function execution(stage, elapsed) {
  if (stage !== 3 && stage !== 5) return null;
  const position = Math.max(0, elapsed - 0.15) / STEP_SECONDS;
  const index = Math.min(NODES.length - 1, Math.floor(position));
  const progress = Math.min(1, position - index);
  return { index, progress, pending: progress < 0.22, finished: position >= NODES.length, refined: stage === 5 };
}

function cardShell(label, badge = "", darkBadge = false) {
  const x = CHAT.contentX;
  const width = CHAT.contentWidth;
  return rect(x, 378, width, 174, COLORS.panel, "#d4d4d4", 10)
    + text(x + 20, 400, label, 11, COLORS.muted, 700)
    + (badge ? pill(x + width - 114, 385, 94, badge, darkBadge) : "")
    + rule(x, 413, x + width, 413);
}

function fields(values, y = 484) {
  return values.map(([label, value], index) => {
    const x = CHAT.contentX + 20 + index * 209;
    return text(x, y, label, 12, COLORS.muted) + text(x, y + 25, value, 19, COLORS.text, 700);
  }).join("");
}

function nodeCard(runState) {
  const { index, pending, refined, progress } = runState;
  const x = CHAT.contentX + 20;
  const ticket = refined ? "Our billing page is down." : "Billing is broken. Our whole team is blocked.";
  const shell = cardShell(`FLOWX RESULT / NODE ${String(index + 1).padStart(2, "0")} OF 05`, pending ? "Running" : "Complete", !pending);
  let content;
  if (pending) {
    content = text(x, 445, NODES[index].label, 23, COLORS.text, 700)
      + text(x, 474, "Rendering this node's result in the conversation...", 16, COLORS.muted)
      + rect(x, 497, 435, 9, COLORS.soft, "none", 4)
      + rect(x, 516, 303, 9, COLORS.soft, "none", 4);
  } else if (index === 0) {
    content = text(x, 443, "Support ticket", 23, COLORS.text, 700)
      + text(x, 474, ticket, 19)
      + pill(x, 506, 117, refined ? "#FX-1043" : "#FX-1042")
      + pill(x + 128, 506, 112, "Text input")
      + text(x + 254, 523, "Ready for classification", 14, COLORS.muted);
  } else if (index === 1) {
    content = text(x, 447, "Billing", 30, COLORS.text, 700)
      + text(x + 136, 445, "Category matched", 16, COLORS.muted)
      + text(x, 478, "Payment or billing access issue. Route to the billing queue.", 17)
      + pill(x, 506, 74, "Billing", true)
      + pill(x + 84, 506, 94, "Account")
      + pill(x + 188, 506, 100, "Technical");
  } else if (index === 2) {
    content = text(x, 447, "Urgent", 30, COLORS.text, 700)
      + text(x, 478, "A team-wide blocker needs immediate attention.", 18)
      + ["Low", "Normal", "High", "Urgent"].map((label, position) => pill(x + position * 97, 506, 87, label, position === 3)).join("")
      + [0, 1, 2, 3].map((bar) => rect(x + 534 + bar * 20, 456 - bar * 7, 12, 14 + bar * 7, COLORS.text, "none", 2)).join("");
  } else if (index === 3) {
    content = text(x, 441, "Draft reply", 21, COLORS.text, 700)
      + lines(x, 470, ["We're sorry billing is blocking your team.", "We'll investigate the issue and follow up with next steps."], 17, COLORS.text, 25)
      + (refined
        ? rect(x, 509, CHAT.contentWidth - 40, 29, COLORS.soft, "none", 5) + text(x + 10, 528, "Escalation note: Flag for urgent billing support review.", 15, COLORS.text, 700)
        : pill(x, 512, 94, "Copy reply") + text(x + 111, 529, "Ready for review", 14, COLORS.muted));
  } else {
    content = text(x, 445, "Ticket triage complete", 23, COLORS.text, 700)
      + fields([["Category", "Billing"], ["Priority", "Urgent"], ["Reply", refined ? "Escalation included" : "Draft ready"]], 474)
      + text(x, 536, refined ? "Updated workflow. New input. Ready to reuse." : "Structured result, ready for the host UI.", 14, COLORS.muted);
  }
  const offset = pending ? 5 * (1 - Math.min(1, progress / 0.22)) : 0;
  return `<g data-node-id="${NODES[index].id}" transform="translate(0 ${offset.toFixed(2)})">${shell}${content}</g>`;
}

function planningCard(stage, elapsed) {
  const x = CHAT.contentX + 20;
  if (stage === 0) {
    return cardShell("WORKFLOW BRIEF", "Intent")
      + text(x, 445, "Support ticket triage", 23, COLORS.text, 700)
      + text(x, 474, "From a request to a reusable execution path.", 17, COLORS.muted)
      + fields([["Input", "Support ticket"], ["Process", "Classify + prioritize"], ["Output", "Draft reply"]], 502);
  }
  if (stage === 1) {
    const complete = elapsed >= 5.5;
    return cardShell("WORKFLOW ARTIFACTS", complete ? "Generated" : "Building", complete)
      + text(x, 445, "ticket_triage", 23, COLORS.text, 700)
      + text(x, 475, "5 nodes. Python code. A runnable FastAPI backend.", 17)
      + rect(x, 497, CHAT.contentWidth - 40, 4, COLORS.soft, "none", 2)
      + rect(x, 497, (CHAT.contentWidth - 40) * Math.min(1, elapsed / 5.5), 4, COLORS.text, "none", 2)
      + text(x, 530, complete ? "Workflow created from your intent." : "Planning the graph / Writing node code / Auditing artifacts...", 15, COLORS.muted);
  }
  if (stage === 2) {
    return cardShell("STEP INPUT", "Inspectable")
      + text(x, 445, "Ticket input", 23, COLORS.text, 700)
      + fields([["Field", "ticket"], ["Type", "Text"], ["Required", "Yes"]], 474)
      + text(x, 536, "Example: Billing is broken. Our whole team is blocked.", 15, COLORS.muted);
  }
  return cardShell("WORKFLOW UPDATE", elapsed >= 3 ? "Updated" : "Refining", elapsed >= 3)
    + text(x, 445, "Draft reply", 23, COLORS.text, 700)
    + text(x, 475, "Add an escalation note for urgent tickets.", 18)
    + rect(x, 497, CHAT.contentWidth - 40, 36, COLORS.soft, "none", 6)
    + text(x + 12, 521, "Urgent ticket → draft reply + escalation note", 17, COLORS.text, 700);
}

function ease(value) {
  const clamped = Math.max(0, Math.min(1, value));
  return clamped * clamped * (3 - 2 * clamped);
}

export function conversationState(time) {
  if (!Number.isFinite(time) || time < 0 || time >= DURATION) {
    throw new RangeError(`Conversation time must be between 0 and ${DURATION} seconds (exclusive).`);
  }
  const messages = [];
  const append = (message) => {
    if (time < message.start) return;
    const fraction = message.start === 0 ? 1 : ease((time - message.start) / STREAM.enterSeconds);
    const height = message.type === "user" ? STREAM.userHeight : STREAM.assistantHeight;
    messages.push({ ...message, height, fraction, visibleHeight: height * fraction });
  };
  append({ id: "intent-user", type: "user", start: 0, stage: 0 });
  append({ id: "intent-assistant", type: "assistant", start: 2.2, stage: 0 });
  append({ id: "generate-assistant", type: "assistant", start: 6, stage: 1 });
  append({ id: "inspect-user", type: "user", start: 13, stage: 2 });
  append({ id: "inspect-assistant", type: "assistant", start: 13.5, stage: 2 });
  for (const stage of [3, 5]) {
    const scene = SCENES[stage];
    append({ id: `${scene.label.toLowerCase()}-user`, type: "user", start: scene.start, stage });
    NODES.forEach((node, index) => {
      append({ id: `${scene.label.toLowerCase()}-${node.id}`, type: "assistant", start: scene.start + 0.15 + index * STEP_SECONDS, stage, nodeIndex: index });
    });
  }
  append({ id: "refine-user", type: "user", start: 28, stage: 4 });
  append({ id: "refine-assistant", type: "assistant", start: 28.5, stage: 4 });
  messages.sort((a, b) => a.start - b.start);
  const totalHeight = messages.reduce((sum, message) => sum + message.visibleHeight, 0);
  return { messages, totalHeight, scroll: Math.max(0, totalHeight - STREAM.height) };
}

function userMessage(message, time) {
  let prompt = SCENES[message.stage].prompt;
  if (message.stage === 0) {
    let remaining = Math.floor(Math.min(1, time / 2) * prompt.join("").length);
    prompt = prompt.map((line) => {
      const visible = line.slice(0, Math.max(0, remaining));
      remaining -= line.length;
      return visible;
    });
  }
  const x = CHAT.contentX + 46;
  return rect(x, 0, CHAT.contentWidth - 46, 70, COLORS.soft, "none", 12)
    + lines(x + 16, prompt.length === 1 ? 42 : 29, prompt, 16, COLORS.text, 23);
}

function assistantMessage(message, time) {
  const { stage, nodeIndex } = message;
  const elapsed = time - SCENES[stage].start;
  const scene = SCENES[stage];
  const nodeElapsed = time - message.start;
  const runState = nodeIndex === undefined ? null : {
    index: nodeIndex,
    pending: nodeElapsed < STEP_SECONDS * 0.22,
    progress: Math.min(1, nodeElapsed / STEP_SECONDS),
    refined: stage === 5,
  };
  const action = runState
    ? `FlowX · ${NODES[nodeIndex].label} · ${runState.pending ? "running" : "complete"}`
    : scene.action;
  const reply = runState ? (runState.pending ? `I'll run the ${NODES[nodeIndex].label.toLowerCase()} node and show its result here.` : NODES[nodeIndex].reply)
    : stage === 1 && elapsed < 5.5 ? "I'm planning the graph and generating its node implementations."
    : stage === 4 && elapsed < 3 ? "Updating the reply logic. The other nodes stay unchanged."
    : scene.reply;
  return text(CHAT.contentX, 14, `›  ${action}`, 12, COLORS.muted)
    + text(CHAT.contentX, 42, reply, 16)
    + `<g transform="translate(0 -316)">${runState ? nodeCard(runState) : planningCard(stage, elapsed)}</g>`;
}

function conversation(time) {
  const { messages, scroll, totalHeight } = conversationState(time);
  let top = STREAM.top - scroll;
  const stream = messages.map((message) => {
    const visible = message.fraction > 0 && top + message.height > STREAM.top && top < STREAM.top + STREAM.height;
    const result = visible ? `<g data-message-id="${message.id}" transform="translate(0 ${top.toFixed(2)})">${message.type === "user" ? userMessage(message, time) : assistantMessage(message, time)}</g>` : "";
    top += message.visibleHeight;
    return result;
  }).join("");
  const scrollbar = totalHeight > STREAM.height
    ? rect(CHAT.x + CHAT.width - 9, STREAM.top, 2, STREAM.height, COLORS.soft, "none", 1)
      + rect(CHAT.x + CHAT.width - 9, STREAM.top + STREAM.height - Math.max(30, STREAM.height * STREAM.height / totalHeight), 2, Math.max(30, STREAM.height * STREAM.height / totalHeight), "#d4d4d4", "none", 1)
    : "";
  return [
    `<g data-stream-message-count="${messages.length}" data-stream-scroll="${scroll.toFixed(2)}" clip-path="url(#conversation-viewport)">${stream}</g>`,
    scroll > 0 ? rect(CHAT.contentX, STREAM.top, CHAT.contentWidth, 22, "url(#stream-edge)", "none", 0) : "",
    scrollbar,
    rect(218, 536, 658, 96, COLORS.panel, "#d4d4d4", 16),
    text(236, 564, "Ask for follow-up changes", 16, COLORS.faint),
    rule(239, 598, 239, 610, COLORS.muted),
    rule(233, 604, 245, 604, COLORS.muted),
    text(257, 609, "Agent", 12),
    text(298, 609, "⌄", 12, COLORS.muted),
    text(319, 609, "Default permissions", 12, COLORS.muted),
    `<circle cx="852" cy="605" r="13" fill="${COLORS.text}"/>`,
    `<path d="M 848 604 L 852 600 L 856 604 M 852 600 L 852 612" stroke="${COLORS.panel}" stroke-width="1.5" fill="none"/>`,
    icon(226, 640, "branch"),
    text(248, 652, "main", 11, COLORS.muted),
    text(711, 652, "FlowX workflow capability", 11, COLORS.muted),
  ].join("");
}

function workflow(stage, elapsed, runState) {
  const x = SIDEBAR.x + 20;
  const version = stage === 5 || (stage === 4 && elapsed >= 3) ? "v2" : "v1";
  const revealed = stage === 0 ? 0 : stage === 1 ? Math.min(5, Math.floor(elapsed / 1.1) + 1) : 5;
  const nodes = NODES.map((node, index) => {
    const y = 179 + index * 55;
    const visible = index < revealed;
    const changed = stage === 4 && index === 3;
    const active = visible && ((runState && index === runState.index)
      || (stage === 1 && index === revealed - 1 && elapsed < 5.5) || changed || (stage === 2 && index === 0));
    const done = runState && (index < runState.index || (index === runState.index && !runState.pending));
    const status = done ? "Done" : active ? (runState ? "Running" : changed ? "Updated" : stage === 2 ? "Input" : "Building") : "";
    return [
      index < 4 ? rule(x + 13, y + 35, x + 13, y + 62, done ? COLORS.text : "#d4d4d4") : "",
      rect(x - 4, y, 188, 44, active ? "#ededed" : COLORS.background, active ? "#d4d4d4" : "none", 8),
      `<circle cx="${x + 13}" cy="${y + 22}" r="8" fill="${done || active ? COLORS.text : COLORS.panel}" stroke="${visible ? COLORS.text : "#d4d4d4"}"/>`,
      done ? check(x + 13, y + 22, COLORS.panel) : text(x + 10, y + 26, index + 1, 10, active ? COLORS.panel : COLORS.faint),
      text(x + 30, y + 19, node.label, 12, visible ? COLORS.text : COLORS.faint, active ? 700 : 400),
      text(x + 30, y + 34, status || (visible ? node.kind : "Not generated yet"), 10, COLORS.muted),
    ].join("");
  }).join("");
  return [
    rect(SIDEBAR.x, 107, SIDEBAR.width - 1, 548, COLORS.background, "none", 0),
    rule(SIDEBAR.x, 106, SIDEBAR.x, 656),
    text(x, 139, "Workflow", 14, COLORS.text, 700),
    pill(x + 131, 123, 49, stage === 0 ? "Idea" : version),
    text(x, 163, "ticket_triage / 5 nodes", 11, COLORS.muted),
    nodes,
    text(x, 473, runState ? (runState.finished ? "Run complete · cards ready" : `Node ${runState.index + 1} of 5 · ${runState.pending ? "running" : "card ready"}`) : stage === 4 ? (elapsed >= 3 ? "Reply node refined" : "Refining reply node") : stage === 0 ? "Ready to generate" : stage === 1 && elapsed < 5.5 ? "Building the workflow" : "Generated by FlowX", 11, COLORS.muted),
    rule(x, 494, x + 180, 494),
    text(x, 520, "Generated files", 11, COLORS.muted, 700),
    ...["workflow.json", "nodes/*.py", "main.py"].map((file, index) => text(x, 546 + index * 25, `↳  ${file}`, 12, stage === 0 ? COLORS.faint : COLORS.muted)),
    text(x, 634, "Powered by FlowX", 11, COLORS.muted),
  ].join("");
}

export function frame(time) {
  if (!Number.isFinite(time) || time < 0 || time >= DURATION) {
    throw new RangeError(`Frame time must be between 0 and ${DURATION} seconds (exclusive).`);
  }
  const stage = SCENES.findIndex((scene) => time >= scene.start && time < scene.end);
  const scene = SCENES[stage];
  const elapsed = time - scene.start;
  const runState = execution(stage, elapsed);
  const timeline = SCENES.map((entry, index) => {
    const x = 28 + index * 185;
    const active = index === stage;
    const done = index < stage;
    return rect(x, 671, 171, 28, active ? COLORS.text : "none", "none", 6)
      + text(x + 10, 690, `${String(index + 1).padStart(2, "0")}  ${entry.label}`, 13, active ? COLORS.panel : done ? COLORS.text : COLORS.muted, active ? 700 : 400);
  }).join("");
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${WIDTH}" height="${HEIGHT}" viewBox="0 0 ${WIDTH} ${HEIGHT}">
  <defs>
    <clipPath id="conversation-viewport"><rect x="${CHAT.x + 18}" y="${STREAM.top}" width="${CHAT.width - 36}" height="${STREAM.height}"/></clipPath>
    <linearGradient id="stream-edge" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="${COLORS.panel}"/><stop offset="1" stop-color="${COLORS.panel}" stop-opacity="0"/></linearGradient>
  </defs>
  ${rect(0, 0, WIDTH, HEIGHT, COLORS.background, "none", 0)}
  ${text(28, 36, "FlowX", 22, COLORS.text, 700)}
  ${text(109, 35, "in a Codex-style conversation", 13, COLORS.muted)}
  ${text(909, 35, "ILLUSTRATIVE CONCEPT / 45s", 11, COLORS.muted)}
  ${rect(28, 58, 1096, 598, COLORS.panel, "#d4d4d4", 12)}
  ${navigation()}
  ${text(218, 88, "Support ticket triage", 15, COLORS.text, 700)}
  ${text(734, 88, "Open  ⌄", 12, COLORS.muted)}
  ${icon(824, 75, "branch")}
  ${icon(856, 75, "panel")}
  ${text(953, 88, "FlowX", 12, COLORS.muted)}
  ${text(1090, 88, "···", 18, COLORS.muted)}
  ${rule(CHAT.x, 106, 1124, 106)}
  ${text(CHAT.contentX, 138, scene.title, 18, COLORS.text, 700)}
  ${text(CHAT.contentX, 160, scene.subtitle, 12, COLORS.muted)}
  ${conversation(time)}
  ${workflow(stage, elapsed, runState)}
  ${timeline}
  ${rect(28, 701, 1096, 2, COLORS.line, "none", 1)}
  ${rect(28, 701, 1096 * (time + 1 / FPS) / DURATION, 2, COLORS.text, "none", 1)}
  ${text(28, 725, "FlowX is building the Generative Workflow layer for AI Agents.", 14, COLORS.text, 700)}
  ${text(963, 725, "Intent → execution → UI", 13, COLORS.muted)}
</svg>`;
}

function run(executable, args) {
  const result = spawnSync(executable, args, { encoding: "utf8", maxBuffer: 16 * 1024 * 1024 });
  if (result.error || result.status !== 0) {
    throw new Error(`${executable} failed: ${result.error?.message || result.stderr || result.stdout}`);
  }
}

function rasterize(directory, name, time) {
  const destination = join(directory, `${name}.png`);
  const renderer = new Resvg(frame(time), { font: { defaultFontFamily: FONT } });
  writeFileSync(destination, renderer.render().asPng());
}

function options() {
  const args = process.argv.slice(2);
  const config = { output: join(ASSETS, "generative-workflow-demo.gif"), preview: null, previewOnly: false };
  for (let index = 0; index < args.length; index += 1) {
    const argument = args[index];
    if (argument === "--preview-only") {
      config.previewOnly = true;
    } else if (argument === "--output" || argument === "--preview-dir") {
      const value = args[++index];
      if (!value || value.startsWith("--")) throw new Error(`${argument} requires a path`);
      config[argument === "--output" ? "output" : "preview"] = resolve(value);
    } else {
      throw new Error(`Unknown argument: ${argument}`);
    }
  }
  if (config.previewOnly && !config.preview) throw new Error("--preview-only requires --preview-dir");
  return config;
}

function main() {
  const config = options();
  try {
    ({ Resvg } = require("@resvg/resvg-js"));
  } catch {
    throw new Error("Install @resvg/resvg-js and expose it via NODE_PATH; see assets/README-demo.md.");
  }
  if (!config.previewOnly) run("ffmpeg", ["-version"]);
  if (config.preview) {
    mkdirSync(config.preview, { recursive: true });
    [3, 12, 16, 27, 34, 43].forEach((time, index) => {
      rasterize(config.preview, `${index + 1}-${SCENES[index].label.toLowerCase()}`, time);
    });
    for (const [name, start] of [["run", 20], ["reuse", 37]]) {
      NODES.forEach((node, index) => {
        rasterize(config.preview, `${name}-${index + 1}-${node.id}`, start + 0.85 + index * STEP_SECONDS);
      });
    }
    console.log(`Six storyboard previews and ten node-card previews: ${config.preview}`);
  }
  if (config.previewOnly) return;

  const temporary = mkdtempSync(join(tmpdir(), "flowx-demo-"));
  try {
    for (let index = 0; index < DURATION * FPS; index += 1) {
      rasterize(temporary, `frame-${String(index).padStart(4, "0")}`, index / FPS);
      if (index % (FPS * 5) === 0) console.log(`Rendering: ${index / FPS}s / ${DURATION}s`);
    }
    mkdirSync(dirname(config.output), { recursive: true });
    run("ffmpeg", [
      "-hide_banner", "-loglevel", "error", "-y", "-framerate", String(FPS),
      "-i", join(temporary, "frame-%04d.png"),
      "-filter_complex", "[0:v]split[a][b];[a]palettegen=max_colors=128:reserve_transparent=0[p];[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle",
      "-loop", "0", "-final_delay", String(100 / FPS), config.output,
    ]);
    console.log(`Rendered ${DURATION}s looping GIF (${WIDTH}x${HEIGHT}, ${FPS} fps): ${config.output}`);
  } finally {
    rmSync(temporary, { recursive: true, force: true });
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    main();
  } catch (error) {
    console.error(error.message);
    process.exitCode = 1;
  }
}