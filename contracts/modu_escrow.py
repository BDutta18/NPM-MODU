from algopy import (
    ARC4Contract,
    Account,
    Asset,
    BoxMap,
    Global,
    Txn,
    UInt64,
    arc4,
    gtxn,
    itxn,
    subroutine,
)

# Minimum balance cost for a single 8-byte (UInt64) credit box:
#   base MBR  = 2 500 µALGO
#   per byte  =   400 µALGO
#   key bytes = len("credit_") + 32 = 39  →  15 600 µALGO
#   val bytes = 8                          →   3 200 µALGO
# Total ≈ 21 300 µALGO.
#
# Must be a plain Python int at module scope — puya cannot compile AlgoPy
# runtime types (UInt64, Bytes …) at module level.  Inside contract methods
# wrap it in UInt64() before use.
CREDIT_BOX_MBR: int = 21_300


class ModuEscrow(ARC4Contract):
    def __init__(self) -> None:
        # ── global state ───────────────────────────────────────────────
        self.provider: Account = Global.zero_address        # API developer
        self.facilitator: Account = Global.zero_address     # modu proxy backend
        self.asset_id: UInt64 = UInt64(0)                  # ASA to settle in
        self.price: UInt64 = UInt64(0)                     # µASA per API call

        # running total the provider can withdraw
        self.provider_balance: UInt64 = UInt64(0)

        # per-consumer prepaid credit (keyed by address)
        self.credit: BoxMap[Account, UInt64] = BoxMap(
            Account, UInt64, key_prefix=b"credit_"
        )

    # ──────────────────────────────────────────────────────────────────
    # Lifecycle
    # ──────────────────────────────────────────────────────────────────

    @arc4.abimethod(create="require")
    def create(
        self,
        provider: Account,
        facilitator: Account,
        asset: Asset,
        price: UInt64,
    ) -> None:
        """Deploy the contract.  Called once; sets immutable parameters."""
        self.provider = provider
        self.facilitator = facilitator
        self.asset_id = asset.id
        self.price = price
        self.provider_balance = UInt64(0)

    # ──────────────────────────────────────────────────────────────────
    # Admin
    # ──────────────────────────────────────────────────────────────────

    @arc4.abimethod
    def opt_in_asset(self) -> None:
        """Opt the app account into the settlement ASA.

        Must be called by the provider once after deployment, before any
        consumer can deposit.  Issues a zero-amount self-transfer to
        complete the ASA opt-in.

        The outer transaction must supply fee = 2 × min_fee to cover the
        inner transaction.
        """
        self._require_provider()
        assert self.asset_id != UInt64(0), "asset not configured"

        itxn.AssetTransfer(
            xfer_asset=self.asset_id,
            asset_receiver=Global.current_application_address,
            asset_amount=UInt64(0),
            fee=UInt64(0),
        ).submit()

    # ──────────────────────────────────────────────────────────────────
    # Consumer path
    # ──────────────────────────────────────────────────────────────────

    @arc4.abimethod
    def deposit(
        self,
        payment: gtxn.AssetTransferTransaction,
        mbr_payment: gtxn.PaymentTransaction,
    ) -> None:
        """Consumer tops up their prepaid credit.

        Must be submitted as an atomic group of **three** transactions:
          [0]  PaymentTransaction  → app address  (covers box MBR if new)
          [1]  AssetTransferTransaction → app address  (the USDC deposit)
          [2]  ApplicationCall (this method)

        For repeat deposits the MBR is already funded; send `mbr_payment`
        with `amount = 0` if the box already exists.  The contract only
        asserts that the payment goes to the app account and the amount is
        non-negative.

        Args:
            payment:     The ASA transfer carrying the consumer's deposit.
            mbr_payment: A ALGO payment covering the box minimum balance
                         requirement for first-time callers.
        """
        # ── validate the ASA leg ───────────────────────────────────────
        assert payment.sender == Txn.sender, "payment sender must match caller"
        assert payment.asset_receiver == Global.current_application_address, (
            "payment must be sent to the app account"
        )
        assert payment.xfer_asset.id == self.asset_id, "wrong asset"
        assert payment.asset_amount > UInt64(0), "deposit must be positive"

        # ── validate the MBR leg ──────────────────────────────────────
        assert mbr_payment.receiver == Global.current_application_address, (
            "MBR payment must go to the app account"
        )
        # For first-time depositors, the MBR payment must be sufficient.
        is_new_consumer = Txn.sender not in self.credit
        if is_new_consumer:
            assert mbr_payment.amount >= UInt64(CREDIT_BOX_MBR), (
                "insufficient MBR payment for new credit box"
            )

        # ── credit the consumer ───────────────────────────────────────
        existing = self.credit.get(Txn.sender, default=UInt64(0))
        self.credit[Txn.sender] = existing + payment.asset_amount

    # ──────────────────────────────────────────────────────────────────
    # Facilitator path (modu proxy backend)
    # ──────────────────────────────────────────────────────────────────

    @arc4.abimethod
    def use_credit(self, consumer: Account) -> None:
        """Debit one API call from a consumer's prepaid credit.

        Called by the facilitator (modu proxy) after it successfully
        forwards a paid API request.  No on-chain asset transfer occurs —
        this is just a box state update, making it cheap enough for
        high-frequency use.

        Args:
            consumer: The account whose credit is debited.
        """
        self._require_facilitator()

        balance = self.credit.get(consumer, default=UInt64(0))
        assert balance >= self.price, "insufficient credit"

        self.credit[consumer] = balance - self.price
        self.provider_balance += self.price

    # ──────────────────────────────────────────────────────────────────
    # Provider path (API developer)
    # ──────────────────────────────────────────────────────────────────

    @arc4.abimethod
    def withdraw(self) -> None:
        """Pull accumulated earnings out as an ASA transfer.

        Caller note: set outer-transaction fee = 2 × min_txn_fee so the
        inner asset transfer (fee=0) is covered by the fee pool.
        """
        self._require_provider()

        amount = self.provider_balance
        assert amount > UInt64(0), "nothing to withdraw"
        self.provider_balance = UInt64(0)

        itxn.AssetTransfer(
            xfer_asset=self.asset_id,
            asset_receiver=self.provider,
            asset_amount=amount,
            fee=UInt64(0),
        ).submit()

    # ──────────────────────────────────────────────────────────────────
    # Read-only helpers
    # ──────────────────────────────────────────────────────────────────

    @arc4.abimethod(readonly=True)
    def credit_balance(self, consumer: Account) -> UInt64:
        """Return the consumer's remaining prepaid credit.

        Marked readonly so the CLI / proxy can simulate this off-chain
        for free (no fee, no round-trip wait for confirmation).
        """
        return self.credit.get(consumer, default=UInt64(0))

    @arc4.abimethod(readonly=True)
    def box_mbr(self) -> UInt64:
        """Return the ALGO minimum balance cost for one credit box.

        Clients call this before a first-time deposit to know exactly
        how much ALGO to send in the MBR payment leg.
        """
        return UInt64(CREDIT_BOX_MBR)

    # ──────────────────────────────────────────────────────────────────
    # Internal guards
    # ──────────────────────────────────────────────────────────────────

    @subroutine
    def _require_facilitator(self) -> None:
        assert Txn.sender == self.facilitator, "unauthorized: not facilitator"

    @subroutine
    def _require_provider(self) -> None:
        assert Txn.sender == self.provider, "unauthorized: not provider"
