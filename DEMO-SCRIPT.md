# Three-minute demo script

Target length: 2:50–2:59. Record at a steady pace and keep the cursor movement deliberate.

## 0:00–0:16 — The problem

**Screen:** A representative production spreadsheet, then cut to Shopfloor.

**Voiceover:**

“Shopify knows what a customer bought. It does not know whether their photo is printable, whether the product has been pressed, or which order is late. For many small makers, that physical workflow still lives in a spreadsheet. Shopfloor is the missing production layer.”

## 0:16–0:31 — Orders arrive

**Screen:** Run the five-order simulation command, then refresh the public dashboard.

```bash
curl -X POST 'https://shopfloor-production-os.onrender.com/simulate/orders?n=5'
```

**Voiceover:**

“This uses the real shape of Shopify's `orders/create` webhook. In production, Shopfloor verifies Shopify's signature. For the demo, five synthetic orders arrive with a controlled mix of customer photos.”

## 0:31–1:22 — GPT-5.6 rejects, then releases

**Screen:** In **On hold photo**, open Lucas Pérez's order with the face too close to the crop. Show the reason and open the customer re-upload page.

**Voiceover:**

“Before anything prints, GPT-5.6 checks sharpness—especially faces—resolution, exposure, and crop risk for a 50-millimeter product. This Madrid shop localizes customer messages in Spanish; here it says the square crop could cut the face and asks for more space around it.”

**Screen:** Upload the prepared blurry replacement. Return to the dashboard, wait for the order to reappear in **On hold photo**, show the new blurry-photo reason, and open the newly generated re-upload link.

“The link is secure, expiring, and single-use. More importantly, a replacement is not automatically accepted. This second image is blurry, so the same vision gate rejects it and explains why.”

**Screen:** Upload the sharp version of the same picture. Return to the dashboard and show Lucas moving to **Ready to print**.

“Now the sharp version passes and the order enters production. If the AI service is unavailable, Shopfloor fails closed—the image stays pending instead of silently passing.”

## 1:22–1:48 — Run production

**Screen:** Advance one order through Printed and Pressed. Open its detail page and timeline.

**Voiceover:**

“The owner taps the physical work forward. Every transition is timestamped. Shopfloor applies business-hour targets, shows the oldest work first, and separates shop-controlled production time from time waiting on the customer or carrier.”

## 1:48–2:08 — Owner operations

**Screen:** Show the top of the Shopfloor owner dashboard, then Datadog's owner dashboard.

**Voiceover:**

“The first question is not server request rate. It is: what needs action now? The owner sees orders and overdue work by stage, then one dominant seven-day production-cycle number. Slow-order, fulfillment, customer-wait, and delivery measures support that answer without competing with it.”

## 2:08–2:35 — Incident copilot

**Screen:** Keep the Shopfloor dashboard open while the prepared Datadog test alert fires off-screen. Within five seconds the incident banner appears; press play there and let one sentence be heard. Do not navigate away.

**Voiceover:**

“When Datadog alerts, Shopfloor collects the live queue, oldest order, and recent application events. GPT-5.6 turns that evidence into exactly three sentences: what is happening, the likely cause, and the next action. ElevenLabs then speaks the briefing, so the owner hears the problem without reading a dashboard.”

## 2:35–2:53 — Codex

**Screen:** Show the primary Codex session and a quick scroll through tests or the build log.

**Voiceover:**

“Codex built this with me end to end: architecture, Shopify security, photo recovery, adversarial tests, Datadog, Render deployment, and the redesign after real shop-floor feedback. The primary session stayed continuous so product decisions and code evolved together.”

## 2:53–2:59 — Close

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
