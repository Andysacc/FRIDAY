// Render model output as text nodes: code and HTML are never executed.
export function splitCodeBlocks(content) {
  const lines = String(content).split(/(?<=\n)/);
  const parts = [];
  let prose = "";
  let block = null;
  for (const line of lines) {
    if (!block) {
      const opening = line.match(/^ {0,3}(`{3,}|~{3,})([^\r\n]*)\r?\n?$/);
      if (opening) {
        if (prose) parts.push({ type: "text", content: prose });
        prose = "";
        block = { type: "code", language: opening[2].trim().split(/\s+/)[0] || "text", content: "", fence: opening[1] };
      } else prose += line;
    } else {
      const closing = line.trim();
      if (closing.length >= block.fence.length && [...closing].every((char) => char === block.fence[0])) {
        parts.push(block);
        block = null;
      } else block.content += line;
    }
  }
  if (block) parts.push(block); // An unfinished fence remains useful during streaming.
  if (prose) parts.push({ type: "text", content: prose });
  return parts;
}

export function renderChatContent(target, content) {
  const fragment = document.createDocumentFragment();
  for (const part of splitCodeBlocks(content)) {
    if (part.type === "text") {
      fragment.append(document.createTextNode(part.content));
      continue;
    }
    const panel = document.createElement("section");
    panel.className = "chat-code-panel";
    panel.setAttribute("aria-label", `${part.language} code`);
    const header = document.createElement("div");
    header.className = "chat-code-header";
    const label = document.createElement("span");
    label.textContent = part.language;
    const copy = document.createElement("button");
    copy.type = "button";
    copy.textContent = "Copy code";
    copy.setAttribute("aria-label", `Copy ${part.language} code`);
    copy.setAttribute("aria-live", "polite");
    const pre = document.createElement("pre");
    pre.tabIndex = 0;
    const code = document.createElement("code");
    code.textContent = part.content;
    pre.append(code);
    copy.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(part.content);
        copy.textContent = "Copied!";
      } catch {
        // Local HTTP/LAN browsers may deny clipboard access. Select for manual copy.
        const range = document.createRange();
        range.selectNodeContents(code);
        const selection = window.getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
        copy.textContent = "Selected — press Ctrl/Cmd+C";
      }
      window.setTimeout(() => { copy.textContent = "Copy code"; }, 2500);
    });
    header.append(label, copy);
    panel.append(header, pre);
    fragment.append(panel);
  }
  target.replaceChildren(fragment);
}
