import { api, apiPost, apiDelete } from "../api.js";
import { showStatus, guardForm, mountAsync, guardedSubmit } from "../config-helpers.js";
import { confirmDialog } from "../ui.js";

// Cache-buster so a freshly uploaded/removed border isn't served stale by the
// browser when we repoint the same <img>.
function imageUrl() {
  return `/api/config/quote-border/image?t=${Date.now()}`;
}

// Copy-paste starter prompt for generating a compatible border with any image
// AI (ChatGPT/DALL·E, Midjourney, Stable Diffusion…). The must-haves — edges-only
// decoration and a clear transparent center — are what the renderer needs to fit
// the quote inside the opening. The frame's own proportions are no longer a
// must-have (the card takes its shape from the frame), but a landscape frame
// still suits a quote best. The style phrase is meant to be swapped.
const GEN_PROMPT =
  "Ornate decorative border frame for a quote card, elegant golden art-nouveau " +
  "florals and filigree in the four corners and along the edges only, with a large " +
  "clear empty space in the center, on a fully transparent background (alpha), wide " +
  "landscape rectangle, symmetrical, high detail, clean " +
  "cutout, no text, no letters, no watermark, no solid background fill.";

// Negative prompt for tools that take one (Stable Diffusion et al.).
const GEN_NEGATIVE =
  "solid background, opaque center, filled middle, text, letters, words, " +
  "watermark, signature, photo, decoration covering the center.";

async function copyToClipboard(text, btn) {
  try {
    await navigator.clipboard.writeText(text);
  } catch (_) {
    // Fallback for non-secure contexts.
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); } catch (_) { /* give up */ }
    ta.remove();
  }
  const prev = btn.textContent;
  btn.textContent = "Copied!";
  setTimeout(() => { btn.textContent = prev; }, 1500);
}

export function mount(container) {
  container.innerHTML = `<div class="panel"><div class="empty">Loading quote tool…</div></div>`;

  return mountAsync(container, async () => {
    // No try/catch on purpose: "no border set" is a 200 with exists:false,
    // so a rejection here is a genuine failure and belongs to mountAsync,
    // which draws it with a working Try again.
    const meta = await api("/api/config/quote-border");

    container.innerHTML = `
      <div class="panel">
        <header>
          <h2>Quote Tool</h2>
          <div class="subtitle">The border framing this server's quote cards</div>
        </header>

        <form class="form card" data-border-form>
          <div class="section-label">Quote Card Border</div>
          <div class="field-hint" style="margin-bottom:1rem">
            Upload a <strong>PNG or WEBP with a see-through center</strong>. The quote and
            avatar are fitted inside the opening.
          </div>

          <div data-preview-wrap style="margin-bottom:1rem;display:none">
            <div class="field-hint" style="margin-bottom:.5rem">Border in use right now</div>
            <img data-preview alt="The quote card border currently in use"
              style="max-width:100%;width:450px;border-radius:8px;background:
              repeating-conic-gradient(#808080 0% 25%, #a0a0a0 0% 50%) 50% / 20px 20px" />
            <div data-dims class="field-hint" style="margin-top:.35rem"></div>
          </div>
          <div data-empty class="field-hint" style="margin-bottom:1rem;display:none">
            No custom border yet. Quote cards use the bundled <em>Golden Poppy</em> frame.
          </div>

          <div class="field">
            <label for="qb-file">Border Image File</label>
            <input type="file" id="qb-file" data-border-file accept="image/png,image/webp" />
            <div class="field-hint">PNG or WEBP, up to 8 MB. Saving replaces the current border on new quote cards.</div>
          </div>
          <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;">
            <button type="submit" class="btn btn-primary">Save Border</button>
            <button type="button" class="btn btn-danger" data-remove
              style="display:none">Remove Border</button>
            <span data-status></span>
          </div>
        </form>

        <section class="form card" style="margin-top:12px;">
          <div class="section-label">No Border Yet? Generate One</div>
          <div class="field-hint" style="margin-bottom:1rem">
            Paste the prompt into any image generator and swap the style. Export a
            <strong>PNG with a clear, transparent center</strong>.
          </div>
          <div class="field">
            <label for="qb-prompt">Prompt</label>
            <textarea id="qb-prompt" data-gen-prompt readonly rows="5"
              style="width:100%;box-sizing:border-box;resize:vertical;font-family:inherit;line-height:1.45"></textarea>
          </div>
          <div style="display:flex;align-items:center;gap:12px;margin-bottom:1rem">
            <button type="button" class="btn btn-secondary" data-copy-prompt>Copy Prompt</button>
          </div>
          <div class="field">
            <label for="qb-negative">Things to Avoid <span style="font-weight:400;color:var(--ink-dim)">(optional — for Stable Diffusion and similar tools)</span></label>
            <textarea id="qb-negative" data-gen-negative readonly rows="2"
              style="width:100%;box-sizing:border-box;resize:vertical;font-family:inherit;line-height:1.45"></textarea>
          </div>
          <div style="display:flex;align-items:center;gap:12px">
            <button type="button" class="btn btn-secondary" data-copy-negative>Copy List</button>
          </div>
        </section>
      </div>
    `;

    const previewWrap = container.querySelector("[data-preview-wrap]");
    const preview = container.querySelector("[data-preview]");
    const dims = container.querySelector("[data-dims]");
    const emptyMsg = container.querySelector("[data-empty]");
    const fileInput = container.querySelector("[data-border-file]");
    const borderForm = container.querySelector("[data-border-form]");
    const removeBtn = container.querySelector("[data-remove]");
    const status = container.querySelector("[data-status]");
    guardForm(borderForm);

    // Generation-prompt helper. Set via .value (not innerHTML) so the text can't
    // break out of the textarea.
    const promptBox = container.querySelector("[data-gen-prompt]");
    const negativeBox = container.querySelector("[data-gen-negative]");
    promptBox.value = GEN_PROMPT;
    negativeBox.value = GEN_NEGATIVE;
    container.querySelector("[data-copy-prompt]").addEventListener("click", (e) => {
      copyToClipboard(GEN_PROMPT, e.currentTarget);
    });
    container.querySelector("[data-copy-negative]").addEventListener("click", (e) => {
      copyToClipboard(GEN_NEGATIVE, e.currentTarget);
    });

    function render(m) {
      if (m && m.exists) {
        preview.src = imageUrl();
        previewWrap.style.display = "block";
        emptyMsg.style.display = "none";
        removeBtn.style.display = "inline-block";
        dims.textContent =
          m.width && m.height ? `${m.width}×${m.height}px` : "";
      } else {
        previewWrap.style.display = "none";
        emptyMsg.style.display = "block";
        removeBtn.style.display = "none";
      }
    }
    render(meta);

    guardedSubmit(borderForm, async () => {
      if (!fileInput.files.length) {
        showStatus(status, false, "Choose a border image file first.");
        fileInput.focus();
        return;
      }
      const fd = new FormData();
      fd.append("file", fileInput.files[0]);
      showStatus(status, true, "Uploading…");
      try {
        const data = await apiPost("/api/config/quote-border", fd);
        fileInput.value = "";
        render(data);
        showStatus(status, true, "Saved");
      } catch (err) {
        showStatus(status, false, err.message);
      }
    });

    removeBtn.addEventListener("click", async () => {
      const ok = await confirmDialog(
        "Delete this server's custom border? Quote cards go back to the bundled Golden Poppy frame, and you'll need the original file to restore it.",
        { title: "Remove Border", danger: true, confirmLabel: "Remove Border" },
      );
      if (!ok) return;
      removeBtn.disabled = true;
      showStatus(status, true, "Removing…");
      try {
        const data = await apiDelete("/api/config/quote-border");
        render(data);
        showStatus(status, true, "Removed");
      } catch (err) {
        showStatus(status, false, err.message);
      } finally {
        removeBtn.disabled = false;
      }
    });
  }, { errorMsg: "Couldn’t load the quote tool settings." });
}
