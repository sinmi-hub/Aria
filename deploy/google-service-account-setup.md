# Google service account setup for Aria (Sheets + Calendar)

A service account is a "robot" Google identity. No logins, no consent screens, no
token expiry — you just create one key and *share* your Sheet and a calendar with its
email. ~10 minutes, once. **No billing required** (Sheets + Calendar APIs are free).

When done, you hand over: the downloaded **JSON key** (placed at a path), the **Sheet ID**,
and the **Calendar ID**. Nothing secret needs to be pasted into chat.

---

## 1. Create (or pick) a Google Cloud project
1. Go to **https://console.cloud.google.com**
2. Top-left **project picker** → **New Project**
3. Name it `your-gcp-project` → **Create** → wait, then **select** it (project picker again).

## 2. Enable the two APIs
1. Left menu (☰) → **APIs & Services → Library**
2. Search **"Google Sheets API"** → open it → **Enable**
3. Back to Library → search **"Google Calendar API"** → open it → **Enable**

## 3. Create the service account
1. **APIs & Services → Credentials**
2. **+ Create Credentials** (top) → **Service account**
3. Service account name: `aria-agent` → **Create and Continue**
4. "Grant this service account access to project" → **skip** (Continue) — *no role needed; we use sharing instead*
5. "Grant users access" → **skip** → **Done**
6. You'll land back on Credentials. Under **Service Accounts**, copy the new email —
   it looks like `aria-agent@your-gcp-project.iam.gserviceaccount.com`. **You'll paste this in steps 5 & 6.**

## 4. Create the JSON key (this is Aria's credential)
1. Click the `aria-agent@…` service account → **Keys** tab
2. **Add Key → Create new key → JSON → Create**
3. A `.json` file downloads. **Treat it like a password.**
4. Move it to: `~/voice-agent/deploy/google-sa.json` (this filename is gitignored).

## 5. Share the leads Sheet with the service account
1. Open your **leads Google Sheet**
2. **Share** (top-right) → paste the `aria-agent@…` email
3. Role: **Editor** → uncheck "Notify people" → **Share**
4. Grab the **Sheet ID** from the URL:
   `https://docs.google.com/spreadsheets/d/`**`<THIS_IS_THE_SHEET_ID>`**`/edit`

## 6. Share the calendar with the service account
1. Go to **https://calendar.google.com**
2. (Recommended) create a dedicated calendar: left sidebar, **Other calendars → +** → **Create new calendar** → name it `Settl Demos` → Create.
3. Hover that calendar in the left list → **⋮ → Settings and sharing**
4. Under **Share with specific people or groups → Add people** → paste the `aria-agent@…` email
5. Permission: **"Make changes to events"** → **Send/Save**
6. On the same page, scroll to **Integrate calendar** → copy the **Calendar ID**
   (a long `…@group.calendar.google.com` for a new calendar).

---

## Hand-off (what to give me)
- ✅ JSON key saved at `~/voice-agent/deploy/google-sa.json`
- ✅ **Sheet ID** (from step 5) + the column layout (e.g. name, phone, status, meeting, notes, recording_url)
- ✅ **Calendar ID** (from step 6)

I'll verify the key (without printing it), drop it on the pod as a mounted secret, and wire
the Sheets + Calendar adapters.

## One honest caveat (not a blocker)
Creating and moving events on the shared `Settl Demos` calendar works out of the box. Having
the service account *email an invite to the lead* can need one extra setting (`sendUpdates`
+ calendar config) — for the pilot the event simply appears on the shared calendar, which is
fine. If you want lead-facing email invites later, I'll handle that when wiring.
