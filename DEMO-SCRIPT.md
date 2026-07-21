# Three-minute demo script

Target length: 2:25–2:30. Record at a steady pace and keep the cursor movement deliberate.

## 0:00–0:10 — The context

**Screen:** One clean title slide: **Shopfloor · The production operating system for personalized-product makers.** Show three questions: **Is the customer's photo printable? What should we make next? Which orders are falling behind?** Add a small footer: **Built with Codex + GPT-5.6.**

**Voiceover:**

“Shopify captures the sale, but photo quality, production, and delays still live in spreadsheets and messages. Shopfloor turns that gap into one operating flow.”

## 0:10–0:22 — Orders arrive

**Screen:** Cut directly to Shopfloor. Say “Let me show you with a real order,” then run the five-order simulation command and refresh the public dashboard.

```bash
curl -X POST 'https://shopfloor-production-os.onrender.com/simulate/orders?n=5'
```

**Voiceover:**

“Using Shopify's real order format, five synthetic orders arrive with a controlled mix of customer photos.”

## 0:22–1:03 — GPT-5.6 rejects, then releases

**Screen:** In **On hold photo**, open Lucas Pérez's order with the face too close to the crop. Show the reason and open the customer re-upload page.

**Voiceover:**

“Before printing, GPT-5.6 checks sharpness, resolution, exposure, and crop risk. This Spanish message says the square crop could cut the face and asks for more space.”

**Screen:** Upload the prepared blurry replacement. Return to the dashboard, wait for the order to reappear in **On hold photo**, show the new blurry-photo reason, and open the newly generated re-upload link.

“The re-upload link is secure and single-use. This replacement is blurry, so the vision gate rejects it again and explains why.”

**Screen:** Upload the sharp version of the same picture. Return to the dashboard and show Lucas moving to **Ready to print**.

“Now the sharp version passes and enters production. If AI is unavailable, the image stays pending instead of silently passing.”

## 1:03–1:22 — Run production

**Screen:** Advance one order through Printed and Pressed. Open its detail page and timeline.

**Voiceover:**

“The owner taps the physical work forward. Every transition is timestamped, with business-hour targets and the oldest work shown first.”

## 1:22–1:38 — Owner operations

**Screen:** Show the top of the Shopfloor owner dashboard, then Datadog's owner dashboard.

**Voiceover:**

“The owner sees what needs action now: orders by stage, overdue work, and the seven-day production cycle, backed by Datadog observability.”

## 1:38–2:01 — Incident copilot

**Screen:** Keep the Shopfloor dashboard open while the prepared Datadog test alert fires off-screen. Within five seconds the incident banner appears; press play there and let one sentence be heard. Do not navigate away.

**Voiceover:**

“When Datadog alerts, Shopfloor collects live operational evidence. GPT-5.6 explains what happened, the likely cause, and the next action. ElevenLabs speaks that briefing, so the owner can act without reading a technical dashboard.”

## 2:01–2:22 — Codex

**Screen:** Show the primary Codex session and a quick scroll through tests or the build log.

**Voiceover:**

“Codex built this with me end to end: architecture, security, tests, deployment, and redesign after real shop-floor feedback. One continuous session kept the product decisions and code evolving together.”

## 2:22–2:30 — Close

**Screen:** Return to Shopfloor's green owner summary.

**Voiceover:**

“Build the specific, claim the general: Shopfloor starts with one maker and becomes the production operating system for personalized commerce.”

## Recording checklist

- Prepare two easy-to-find files before recording: one blurry replacement and a sharp version of the same picture
- Reset the hosted demo, then confirm Lucas Pérez starts in **On hold photo** with the crop-risk example
- Put Datadog in Live mode and prepare the test alert before recording
- Use synthetic names and photos only
- Hide API keys, browser account menus, email addresses, and Render environment values
- Keep the business name out of every frame
- Translate every visible Spanish customer message immediately in the English voiceover
- Show both Codex and GPT-5.6 usage verbally
- Do not claim real Shopify traffic, real customer messages, carrier integration, or outbound phone calls
- Upload the final video publicly to YouTube and keep it under three minutes
