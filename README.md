# modu-escrow

> **ModuEscrow** is the V2 on-chain settlement contract for the [modu](https://github.com/modu-dev) pay-per-call API proxy.  
> Instead of a full ASA transfer per API call (MVP path), consumers pre-fund an on-chain credit balance. The modu proxy backend calls `use_credit` — a cheap app-call — after each forwarded request.

---

## Architecture

```
Consumer ──deposit(ASA + MBR)──▶ ModuEscrow (box: credit[consumer])
                                          │
Facilitator ──use_credit(consumer)──▶  credit[consumer] -= price
                                       provider_balance  += price
                                          │
Provider ──withdraw()────────────▶ ASA transfer back to provider
```

| Role | Description |
|---|---|
| **Provider** | The API developer who registered their endpoint with modu |
| **Facilitator** | The modu proxy backend — only address allowed to call `use_credit` |
| **Consumer** | An agent or user calling the protected API endpoint |

---

## Quick Start

### Prerequisites

```bash
pip install algokit-utils algorand-python algorand-python-testing pytest
algokit localnet start
```


### Compile (Windows — algokit CLI not required)

```powershell
# One-time venv setup:
python -m venv .venv
.venv\Scripts\pip install puyapy py-algorand-sdk

# Compile (set VIRTUAL_ENV so puyapy resolves python3 correctly):
$env:VIRTUAL_ENV = "$PWD\.venv"
.venv\Scripts\puyapy.exe contracts/modu_escrow.py --out-dir artifacts/
```

### Test

```bash
pytest tests/ -v
```

### Deploy to LocalNet

```bash
python scripts/deploy.py
```

Set environment variables to use existing accounts/assets:

```bash
export PROVIDER_MNEMONIC="word word word ..."
export FACILITATOR_MNEMONIC="word word word ..."
export ASSET_ID=12345         # existing USDC ASA on LocalNet
export PRICE=10000            # 0.01 USDC per API call
python scripts/deploy.py
```

### Deploy to TestNet

```bash
export DEPLOYER_MNEMONIC="word1 word2 ... word25"
# Optional: reuse an existing ASA
export ASSET_ID=771550951
python scripts/deploy_testnet.py
```

---

## Live TestNet Deployment

| | |
|---|---|
| **App ID** | `771550982` |
| **App Address** | `HD4UT6SCBWGBHR3YUFCLLHZEMD3EVGOONUIFHUAQA4G3D2KIU7FX5QWQVE` |
| **ASA (mock USDC)** | `771550951` |
| **Price per call** | `10 000 µASA (0.01 USDC)` |
| **Network** | Algorand TestNet |
| **Explorer** | [View on Lora](https://lora.algokit.io/testnet/application/771550982) |

---

## ABI Reference

### `create(provider, facilitator, asset, price)` — `[create]`
Deploys the contract. Called once. Sets immutable parameters.

| Param | Type | Description |
|---|---|---|
| `provider` | `address` | API developer wallet |
| `facilitator` | `address` | modu proxy backend address |
| `asset` | `asset` | ASA to settle in (e.g. USDC) |
| `price` | `uint64` | µASA charged per API call |

---

### `opt_in_asset()` — provider only
Opts the app account into the settlement ASA. Must be called **once after deployment** before any consumer can deposit.  

> **Caller note:** set `fee = 2 × min_fee` (covers the inner zero-amount self-transfer).

---

### `deposit(payment, mbr_payment)`
Consumer tops up their prepaid credit. Submit as an **atomic group of 3 transactions**:

```
[0] PaymentTxn  →  app_address  (ALGO to cover box MBR — first deposit only; send 0 on repeats)
[1] AssetTransferTxn  →  app_address  (USDC deposit)
[2] ApplicationCall  →  deposit(payment=txn[1], mbr_payment=txn[0])
```

> First-time depositors must send ≥ **21 300 µALGO** in the MBR payment.  
> Call `box_mbr()` to read the exact amount programmatically.

---

### `use_credit(consumer)` — facilitator only
Debits `price` µASA from the consumer's credit box and adds it to `provider_balance`.  
No on-chain asset transfer — state update only, making it cheap for high-frequency use.

---

### `withdraw()` — provider only
Transfers `provider_balance` µASA to the provider wallet and zeroes the balance.

> **Caller note:** set `fee = 2 × min_fee` (covers the inner ASA transfer).

---

### `credit_balance(consumer) → uint64` — readonly
Returns the consumer's remaining prepaid credit (µASA).  
Can be **simulated off-chain for free** (no transaction fee).

---

### `box_mbr() → uint64` — readonly
Returns the ALGO minimum balance cost for a new credit box.  
Clients should call this before a first-time deposit.

---

## Key Design Notes

### Why `fee = 0` on inner transactions?
Inner transactions set `fee = 0` so the outer app-call transaction covers all fees via the fee pool. The outer transaction must set `fee = N × min_fee` where N = 1 + (number of inner transactions).

### Irreversible payments
USDC transfers on Algorand are final. Verify your endpoint is live and correctly priced before accepting consumer deposits. modu does not offer refunds — document this clearly to your users.

### No private key custody
The contract stores only public wallet addresses. The CLI never holds or signs on behalf of any party.

### Box MBR reclaim
If a consumer's credit is fully consumed, their box still exists and holds the MBR ALGO. A future `close_box` method (V3) can refund the MBR when a consumer opts out.

---

## Project Structure

```
modu-escrow/
├── contracts/
│   └── modu_escrow.py          # ARC4 smart contract (AlgoPy / puyapy)
├── artifacts/                  # Generated by puyapy compile
│   ├── ModuEscrow.approval.teal
│   ├── ModuEscrow.clear.teal
│   └── ModuEscrow.arc56.json
├── tests/
│   └── test_modu_escrow.py     # Pytest suite (no network needed)
├── scripts/
│   ├── deploy.py               # LocalNet deployment helper (algokit_utils)
│   └── deploy_testnet.py       # TestNet deployment (plain py-algorand-sdk + ATC)
├── deployed.env                # Live deployment record (app ID, address, ASA)
├── pyproject.toml
└── README.md
```

---

## Roadmap

| Version | Feature |
|---|---|
| V2 (this) | Credit escrow, session-token settlement, `use_credit` |
| V3 | `update_price` / `update_facilitator` admin methods, `close_box` MBR reclaim, multi-asset support |
