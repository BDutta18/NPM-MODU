"""
deploy.py — LocalNet deployment script for ModuEscrow.

Usage
-----
  # Start LocalNet first:
  algokit localnet start

  # Then run (from repo root):
  python scripts/deploy.py

Environment variables (optional overrides)
------------------------------------------
  ALGOD_ADDRESS   default: http://localhost:4001
  ALGOD_TOKEN     default: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
  PROVIDER_MNEMONIC    25-word mnemonic for the API provider wallet
  FACILITATOR_MNEMONIC 25-word mnemonic for the modu proxy wallet
  ASSET_ID        integer ASA id already created on LocalNet
  PRICE           integer µASA per API call (default: 10_000 = 0.01 USDC)

If PROVIDER_MNEMONIC / FACILITATOR_MNEMONIC are not set the script
generates fresh accounts funded from the LocalNet dispenser.

Outer-transaction fee note
--------------------------
  `opt_in_asset` and `withdraw` each submit one inner transaction with
  fee=0.  The script sets sp.fee = 2 * sp.min_fee for those calls so the
  inner fee is covered by the fee pool.
"""

import os
import sys

from algosdk import account as algo_account
from algosdk import mnemonic
from algosdk.transaction import ApplicationCreateTxn, ApplicationNoOpTxn, wait_for_confirmation
from algosdk.v2client import algod
from algokit_utils import ApplicationClient, ApplicationSpecification

# ── constants ────────────────────────────────────────────────────────────────

ALGOD_ADDRESS = os.getenv("ALGOD_ADDRESS", "http://localhost:4001")
ALGOD_TOKEN = os.getenv(
    "ALGOD_TOKEN",
    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
)
DEFAULT_PRICE = int(os.getenv("PRICE", "10000"))   # 0.01 USDC (6 decimals)


def get_client() -> algod.AlgodClient:
    return algod.AlgodClient(ALGOD_TOKEN, ALGOD_ADDRESS)


def get_or_create_account(client: algod.AlgodClient, mnemonic_env: str, label: str):
    """Return (private_key, address).  Creates + funds if mnemonic not set."""
    raw = os.getenv(mnemonic_env)
    if raw:
        private_key = mnemonic.to_private_key(raw)
        address = algo_account.address_from_private_key(private_key)
        print(f"  {label}: {address} (loaded from env)")
        return private_key, address

    private_key, address = algo_account.generate_account()
    print(f"  {label}: {address} (freshly generated)")
    # Fund from LocalNet dispenser
    dispenser_pk = os.getenv("DISPENSER_MNEMONIC")
    if dispenser_pk:
        _fund_from_dispenser(client, mnemonic.to_private_key(dispenser_pk), address)
    else:
        print(
            f"  ⚠  No DISPENSER_MNEMONIC set — fund {address} manually before continuing."
        )
    return private_key, address


def _fund_from_dispenser(client, dispenser_pk, recipient, amount=10_000_000):
    from algosdk.transaction import PaymentTxn
    sp = client.suggested_params()
    dispenser_addr = algo_account.address_from_private_key(dispenser_pk)
    txn = PaymentTxn(dispenser_addr, sp, recipient, amount)
    signed = txn.sign(dispenser_pk)
    txid = client.send_transaction(signed)
    wait_for_confirmation(client, txid, 4)
    print(f"  Funded {recipient} with {amount / 1e6:.2f} ALGO")


def get_or_create_asset(client: algod.AlgodClient, creator_pk: str, creator_addr: str) -> int:
    """Use ASSET_ID if set, otherwise create a mock USDC on LocalNet."""
    asset_id_env = os.getenv("ASSET_ID")
    if asset_id_env:
        asset_id = int(asset_id_env)
        print(f"  Using existing ASA: {asset_id}")
        return asset_id

    from algosdk.transaction import AssetCreateTxn
    sp = client.suggested_params()
    txn = AssetCreateTxn(
        sender=creator_addr,
        sp=sp,
        total=1_000_000_000_000,  # 1 M USDC (6 decimals)
        decimals=6,
        default_frozen=False,
        unit_name="USDC",
        asset_name="Mock USDC",
    )
    signed = txn.sign(creator_pk)
    txid = client.send_transaction(signed)
    confirmed = wait_for_confirmation(client, txid, 4)
    asset_id = confirmed["asset-index"]
    print(f"  Created mock USDC ASA: {asset_id}")
    return asset_id


def deploy():
    print("\n── ModuEscrow deployment ─────────────────────────────────────────")
    client = get_client()

    print("\n[1] Accounts")
    provider_pk, provider_addr = get_or_create_account(client, "PROVIDER_MNEMONIC", "provider")
    facilitator_pk, facilitator_addr = get_or_create_account(
        client, "FACILITATOR_MNEMONIC", "facilitator"
    )

    print("\n[2] Asset")
    asset_id = get_or_create_asset(client, provider_pk, provider_addr)

    print("\n[3] Deploy contract")
    # Load ARC-32 spec if available; otherwise use the raw contract
    arc32_path = os.path.join(
        os.path.dirname(__file__), "..", "artifacts", "ModuEscrow.arc32.json"
    )
    if not os.path.exists(arc32_path):
        print(
            "  ⚠  artifacts/ModuEscrow.arc32.json not found.\n"
            "     Run `algokit compile py contracts/modu_escrow.py` first."
        )
        sys.exit(1)

    app_spec = ApplicationSpecification.from_json(open(arc32_path).read())
    app_client = ApplicationClient(
        algod_client=client,
        app_spec=app_spec,
        signer=provider_pk,
        sender=provider_addr,
    )

    app_id, app_addr, _ = app_client.create(
        "create",
        provider=provider_addr,
        facilitator=facilitator_addr,
        asset=asset_id,
        price=DEFAULT_PRICE,
    )
    print(f"  App ID:      {app_id}")
    print(f"  App address: {app_addr}")

    print("\n[4] Fund app account (MBR)")
    from algosdk.transaction import PaymentTxn
    sp = client.suggested_params()
    fund_txn = PaymentTxn(provider_addr, sp, app_addr, 200_000)
    signed = fund_txn.sign(provider_pk)
    txid = client.send_transaction(signed)
    wait_for_confirmation(client, txid, 4)
    print("  Sent 0.2 ALGO MBR to app account")

    print("\n[5] Opt app into ASA (fee = 2× min)")
    sp = client.suggested_params()
    sp.fee = sp.min_fee * 2
    sp.flat_fee = True
    app_client.call("opt_in_asset", transaction_parameters={"suggested_params": sp})
    print("  App opted into ASA ✓")

    print("\n── Deployment complete ───────────────────────────────────────────")
    print(f"  App ID:         {app_id}")
    print(f"  App address:    {app_addr}")
    print(f"  Provider:       {provider_addr}")
    print(f"  Facilitator:    {facilitator_addr}")
    print(f"  ASA:            {asset_id}")
    print(f"  Price per call: {DEFAULT_PRICE} µASA ({DEFAULT_PRICE/1e6:.6f} USDC)")


if __name__ == "__main__":
    deploy()
