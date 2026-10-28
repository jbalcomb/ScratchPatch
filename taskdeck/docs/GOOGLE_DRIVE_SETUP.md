# Google Drive for TaskDeck: one-time setup

TaskDeck can save extracted archives to your Google Drive (Extract Archive → Extract to → Google Drive).
For that, TaskDeck signs in as **you** (OAuth). Google requires each app to have its own sign-in "client",
which you create once in the Google Cloud project you already have (`scratchpatch`, the one that holds
the Daily Log's service account).

Time: about 10 minutes. Do it on a desktop browser.

## What TaskDeck will be allowed to do

Only the **`drive.file`** permission: TaskDeck can create folders and files in your Drive and see **only
what it created itself**. It can't read, list or change anything else in your Drive. (That's why the
Drive tab in TaskDeck only shows folders TaskDeck made.) Full Drive browsing is planned separately; it
needs a different sign-in method and a Google review.

## Steps

All in https://console.cloud.google.com with the **scratchpatch** project selected (project picker at
the top of the page).

### 1. Turn on the Google Drive API

1. Menu (☰) → **APIs & Services** → **Library**.
2. Search **Google Drive API** → open it → **Enable**.

### 2. Set up the OAuth consent screen

This is the screen Google shows when you approve TaskDeck. (Google calls this area **Google Auth
Platform**; older consoles call it **OAuth consent screen**.)

1. Menu → **APIs & Services** → **OAuth consent screen** (or **Google Auth Platform** → **Branding**).
2. If asked for the user type: **External**.
3. App name: **TaskDeck**. User support email and developer contact email: your Gmail address.
   Everything else can stay empty. Save.
4. **Data access** (or **Scopes** → **Add or remove scopes**): add
   `https://www.googleapis.com/auth/drive.file` ("See, edit, create, and delete only the specific Google
   Drive files you use with this app"). Save.
5. **Audience** (or the consent screen summary) → **Publish app** → confirm **In production**.

Why publish: while the app is in **Testing**, Google makes every sign-in expire after **7 days** and you'd
have to sign in again weekly. `drive.file` is a non-sensitive permission, so publishing doesn't need a
Google review. When you sign in, Google may still say "Google hasn't verified this app": that's expected
for a personal app; choose **Advanced → Go to TaskDeck (unsafe)** — it's your own app.

### 3. Create the sign-in client

1. Menu → **APIs & Services** → **Credentials** (or **Google Auth Platform** → **Clients**).
2. **Create credentials** → **OAuth client ID** (or **Create client**).
3. Application type: **TVs and Limited Input devices**. Name: **TaskDeck phone**. **Create**.
4. Google shows a **Client ID** (ends in `.apps.googleusercontent.com`) and a **Client secret**.
   Keep this page open, or download the JSON.

Why this type: it gives the "enter a code at google.com/device" sign-in, which TaskDeck can do without
any extra native code on the phone.

### 4. Enter them in TaskDeck (on the phone)

1. TaskDeck → **Settings** tab → **Accounts** → **Google Drive** → **Set up**.
2. Paste the **Client ID** and **Client secret** (the Paste buttons help). They are kept with your other
   credentials on the phone, never in the repository.
3. Tap **Save and connect**. TaskDeck shows a short code (like `BQKD-WXTL`) with **Copy code** and **Open
   google.com/device**.
4. On google.com/device (phone or desktop), enter the code, pick your Google account, approve.
5. TaskDeck notices by itself and shows **Connected as you@gmail.com**.

Don't paste the client secret into chats or commit it. (For this client type Google treats it as not truly
secret, but there's no reason to spread it.)

## Using it

Extract Archive → **Extract to** → **Change…** → **Google Drive** tab → pick a folder TaskDeck created, or
keep the default new folder named after the archive → **Use this** → **Extract**.

TaskDeck unpacks into a temporary folder on the phone, uploads the files, and deletes the temporary copy as
it goes. So the phone needs free space for the unpacked files. While it works, TaskDeck shows a notification
with the progress and keeps going when you use other apps (allow notifications and "unrestricted" battery when
asked, or later in Settings → Background work). If the connection still drops, the upload **pauses**
("Paused: no connection") and carries on from where it stopped.

Running the same extraction again into the same folder (e.g. after a cancel or a crash) skips files that are
already there with the same name and size, and only uploads what's missing or different. Reloading TaskDeck
while a task runs asks first, and by default waits until the task finishes.

## Disconnecting

* In TaskDeck: Settings → Accounts → Google Drive → **Disconnect** (forgets and revokes the sign-in).
* From Google's side: https://myaccount.google.com/permissions → TaskDeck → **Remove access**.

Files TaskDeck already uploaded stay in your Drive.

## Troubleshooting

| What you see | Meaning / fix |
|---|---|
| `invalid_scope` when connecting | Google doesn't allow `drive.file` for this client type after all. Tell Claude; the fallback is a different sign-in method. |
| `invalid_client` | Client ID or secret mistyped, or the client isn't the "TVs and Limited Input devices" type. Re-paste them. |
| `access_denied` | You chose Cancel on the approval page, or your account isn't allowed (consent screen still in Testing without you as a test user). Publish the app (step 2.5) and connect again. |
| "Google Drive API has not been used in project …" | Step 1 not done (or done in another project). |
| "No connection right now. Still waiting…" while you approve | Normal: Android limits TaskDeck's network while you're in the browser. Come back to TaskDeck; it finishes by itself. (Older versions showed "Can't reach oauth2.googleapis.com [Errno 7]" and gave up: tap Connect again.) |
| Asked to sign in again after a week | The consent screen is still in **Testing**. Publish it (step 2.5). |
| "Paused: no connection" while uploading | TaskDeck is in the background, or the phone is offline. Open TaskDeck; the upload continues by itself. (Older versions failed with "Lost the connection to Google Drive while uploading.") |
