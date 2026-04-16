# ECSA Voting System

A lightweight, token-based weighted voting system built with Flask and SQLite. Supports role-based vote weights, multi-category ballots, and admin management.

## ✨ Features

- 🎫 **One-time Token Authentication** — Each voter receives a unique token that can only be used once
- ⚖️ **Weighted Voting** — Different roles have different vote weights (configurable)
  - Chair: ×5
  - Minister: ×2
  - Member: ×1
- 🗂️ **Multi-category Ballots** — Configure multiple election categories, each with its own candidates and selection limit
- 🔐 **Admin Dashboard** — Password-protected management panel
- 📊 **Real-time Results** — Admin preview during voting, public results after closing, grouped by category
- 📁 **CSV Import/Export** — Import candidates and tokens, export results
- 🎨 **Modern UI** — Clean, responsive design with Chinese language support

## 🛠️ Tech Stack

- **Backend:** Python / Flask
- **Database:** SQLite
- **Frontend:** Vanilla HTML/CSS (no framework)
- **Tunnel:** Cloudflare Tunnel (optional, for public access)

## 📦 Prerequisites

- Python 3.8+
- pip

## 🚀 Quick Start (Local)

### 1. Clone the repository

```bash
git clone https://github.com/yourusername/ecsa-voting-system.git
cd ecsa-voting-system
```

### 2. Install dependencies

```bash
pip install flask python-dotenv qrcode[pil]
```

Or create a `requirements.txt`:

```
flask
python-dotenv
qrcode[pil]
```

Then run:

```bash
pip install -r requirements.txt
```

### 3. Configure ballot categories and candidates

Edit `ballot_rules.csv` to define each voting category, how many people each voter must select in that category, and the role-based weight used in that category:

```csv
category_key,category_label,max_choices,chair_weight,minister_weight,member_weight
presidium,Presidium 主席团,2,5,3,1
academic_career_development,Chair of Academic and Career Development 学术与职业发展部部长,2,3,2,1
publicity,Chair of Publicity 宣传部部长,2,3,2,1
events_programming,Chair of Events Programming 活动部部长,2,3,2,1
external_affairs,Chair of External Affairs 外联部部长,2,3,2,1
```

Then edit `candidates.csv` to assign candidates to each category:

```csv
category_key,candidate
presidium,Candidate A
presidium,Candidate B
academic_career_development,Candidate C
publicity,Candidate D
events_programming,Candidate E
external_affairs,Candidate F
```

Rules:

- `category_key` must match between `ballot_rules.csv` and `candidates.csv`
- `max_choices` means the exact number of candidates each voter must select in that category
- `chair_weight`, `minister_weight`, and `member_weight` control how much each role counts in that category
- Each category must have at least one candidate
- `max_choices` cannot be greater than the number of candidates in that category

### 4. Configure tokens (Optional)

If you want to use preset tokens, edit `preset_tokens.csv`:

```csv
token,role,note
C-YOURTOKEN1234567,chair,Chair 1
M-YOURTOKEN2345678,minister,Minister 1
U-YOURTOKEN3456789,member,Member 1
```

The preset token file no longer needs a `weight` column. Default token weight is inferred from `role`, while the actual vote weight for each category comes from `ballot_rules.csv`.

If this file is empty or doesn't exist, tokens will be auto-generated.

### 5. Set admin password

```bash
# Linux/macOS
export ECSA_ADMIN_PASSWORD=your_secure_password

# Windows (PowerShell)
$env:ECSA_ADMIN_PASSWORD="your_secure_password"

# Windows (CMD)
set ECSA_ADMIN_PASSWORD=your_secure_password
```

### 6. Run the application

```bash
python app.py
```

The app will start at `http://localhost:8080/votes`

Admin panel: `http://localhost:8080/votes/admin?pw=your_password`

---

## 🌐 Public Access with Cloudflare Tunnel

To make your local voting system accessible via a public URL (e.g., `vote.yourdomain.com`), you can use Cloudflare Tunnel.

### Prerequisites

- A Cloudflare account (free)
- A domain added to Cloudflare

### Step 1: Install cloudflared

**macOS:**
```bash
brew install cloudflared
```

**Windows:**

Download from [Cloudflare Downloads](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/downloads/) and add to PATH.

**Linux (Debian/Ubuntu):**
```bash
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main" | sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt update && sudo apt install cloudflared
```

**Linux (RPM-based):**
```bash
wget -q https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-x86_64.rpm
sudo rpm -i cloudflared-linux-x86_64.rpm
```

Verify installation:
```bash
cloudflared --version
```

### Step 2: Authenticate with Cloudflare

```bash
cloudflared tunnel login
```

This will open a browser window. Log in and select your domain.

### Step 3: Create a tunnel

```bash
cloudflared tunnel create vote-emorycsa
```

Note the **Tunnel UUID** from the output.

### Step 4: Configure the tunnel

Create `~/.cloudflared/config.yml`:

```yaml
tunnel: <YOUR-TUNNEL-UUID>
credentials-file: /path/to/.cloudflared/<YOUR-TUNNEL-UUID>.json

ingress:
  - hostname: vote.yourdomain.com
    service: http://localhost:8080
  - service: http_status:404
```

Replace:
- `<YOUR-TUNNEL-UUID>` with your actual tunnel UUID
- `vote.yourdomain.com` with your desired subdomain
- Update the credentials-file path accordingly

### Step 5: Create DNS record

```bash
cloudflared tunnel route dns vote-emorycsa vote.yourdomain.com
```

### Step 6: Run the tunnel

```bash
cloudflared tunnel run vote-emorycsa
```

Your voting system is now accessible at `https://vote.yourdomain.com/votes`

### (Optional) Run as a service

**Linux:**
```bash
sudo cloudflared service install
sudo systemctl start cloudflared
sudo systemctl enable cloudflared
```

**macOS:**
```bash
sudo cloudflared service install
sudo launchctl start com.cloudflare.cloudflared
```

---

## 🔄 Running on a Different Device

If you want to run this system on a different computer:

### For local-only access:
Simply clone the repo and follow the Quick Start steps. It will work on `localhost:8080`.

### For public access via Cloudflare Tunnel:

**Option A: Create a new tunnel on the new device**
1. Install `cloudflared` on the new device
2. Run `cloudflared tunnel login`
3. Create a new tunnel or use the remotely-managed tunnel from Cloudflare Dashboard

**Option B: Migrate existing tunnel**
1. Copy the credentials file (`~/.cloudflared/<UUID>.json`) to the new device
2. Copy `~/.cloudflared/config.yml` to the new device
3. Install `cloudflared` and run `cloudflared tunnel run <tunnel-name>`

**Option C: Use Cloudflare Dashboard (Recommended)**
1. Go to [Cloudflare Zero Trust](https://one.dash.cloudflare.com/) → Networks → Tunnels
2. Select your tunnel → Configure
3. Get the install command with token for the new device
4. Run the command on your new device

---

## 📁 File Structure

```
ecsa-voting-system/
├── app.py                 # Main application
├── candidates.csv         # Candidate list (SAMPLE - replace with your own)
├── preset_tokens.csv      # Preset tokens (SAMPLE - replace with your own)
├── votes.db              # SQLite database (auto-generated, DO NOT commit)
├── exports/              # Exported CSV files (DO NOT commit)
├── requirements.txt      # Python dependencies
├── LICENSE               # MIT License
└── README.md             # This file
```

## ⚙️ Configuration

All configuration is in the `AppConfig` class in `app.py`:

| Setting | Default | Description |
|---------|---------|-------------|
| `PUBLIC_BASE_URL` | `https://vote.emorycsa.org/votes` | Public URL for QR codes |
| `HOST` | `0.0.0.0` | Flask listen address |
| `PORT` | `8080` | Flask listen port |
| `WEIGHT_CHAIR` | `5` | Vote weight for chairs |
| `WEIGHT_MINISTER` | `2` | Vote weight for ministers |
| `WEIGHT_MEMBER` | `1` | Vote weight for members |
| `NUM_CHAIR` | `3` | Number of chair tokens (auto-gen mode) |
| `NUM_MINISTER` | `5` | Number of minister tokens (auto-gen mode) |
| `NUM_MEMBER` | `60` | Number of member tokens (auto-gen mode) |

## 🔒 Security Notes

- **Change the default admin password** before deployment
- **Never commit** `votes.db`, real `preset_tokens.csv`, or `exports/` to Git
- The sample CSV files in this repo are for demonstration only
- Consider using HTTPS (Cloudflare Tunnel provides this automatically)

## 📋 Admin Workflow

1. **Before voting:** Generate/import tokens, distribute to voters
2. **Start voting:** Click "开启投票" in admin panel
3. **During voting:** Monitor participation (optional)
4. **End voting:** Click "结束投票" in admin panel
5. **After voting:** View and export results

## 🤝 Contributing

Pull requests are welcome. For major changes, please open an issue first.

## 📄 License

[MIT](LICENSE)
