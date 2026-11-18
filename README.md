# Deal Finder (cloud version)

Search for deals from your phone. Each search runs on GitHub's free servers (about a minute), so your phone isn't doing the work.
You can type any search, save it to a watchlist, and scan the whole watchlist with one tap.

- **Search tab**: type anything, then tap Search. Results show price, quantity, price per unit and estimated profit.
- **Deals tab**: everything your saved searches have found. Tap **Scan watchlist** to check them all again.
- **Watchlist tab**: edit or delete saved searches.

Optional automatic daily scan: open `.github/workflows/scan.yml` and remove the `#` from the two `schedule` lines.

---

## Setup (all on your phone, about 10 minutes, one time)

### 1. Get the files into a GitHub repository
**Easiest:** create a free account at github.com, then create an **empty public repository** called `deal-finder`.
Connect GitHub to Claude (Settings → Connectors), and Claude can upload everything for you.

### 2. Turn on the web page
In the repository: **Settings → Pages**. Under *Build and deployment*, choose:
**Deploy from a branch** → branch **main** → folder **/docs** → **Save**.
After a minute or two your app is at `https://YOUR-USERNAME.github.io/deal-finder/`.

### 3. Make an access token (lets the page start searches)
Your profile picture → **Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token**.
- Repository access: **Only select repositories** → `deal-finder`
- Repository permissions: **Actions: Read and write**, **Contents: Read and write**
- Generate, then copy the token.

### 4. Connect
Open your app page → **Setup** tab → paste the token → **Save & test**. You should see ✅ Connected.
In Chrome, choose ⋮ → **Add to Home screen**, and it opens like an app.

### 5. Optional but recommended: eBay
Most bulk lots are on eBay. Get free keys at developer.ebay.com → *Application Keys* → **Production** keyset.
Then in the repository: **Settings → Secrets and variables → Actions → New repository secret**. Add two secrets:
`EBAY_CLIENT_ID` (the App ID) and `EBAY_CLIENT_SECRET` (the Cert ID).

---

## Good to know
- **Sources:** Slickdeals (works out of the box) and eBay (needs the free keys in step 5).
  Reddit isn't included because it blocks GitHub's servers.
  If a source fails, the error shows above the results, and **See log** opens the full run on GitHub.
- **Cost:** GitHub Actions is free for public repositories.
- **Privacy:** the repository is public (GitHub Pages is free only for public repos).
  Anyone with the link could see your watchlist and the deals it found.
  Your token stays only on your phone, and eBay keys are stored as secrets, so neither is public.
- **Profit** = resells-for × quantity × (1 − fee %) − price − shipping. The fee is in Setup (default 13.25%, roughly eBay's).
- The token expires on the date you chose. When it does, make a new one and paste it into Setup.
