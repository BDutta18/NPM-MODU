# Changelog

## [0.1.0] - 2026-09-12

### Added
- ModuEscrow ARC4 smart contract (contracts/modu_escrow.py)
  - BoxMap-based per-consumer credit storage
  - create(), deposit(), use_credit(), withdraw() ABI methods
  - opt_in_asset() for ASA self-opt-in (fee pool pattern)
  - Readonly credit_balance() and box_mbr() helpers
- Compiled TEAL artifacts (puyapy 5.10.1, AVM 11)
- LocalNet deployment script (scripts/deploy.py) via algokit_utils
- TestNet deployment script (scripts/deploy_testnet.py) via AtomicTransactionComposer
- Pytest suite for all contract methods (tests/test_modu_escrow.py)

### Fixed
- ABI method descriptor: Asset args compiled as uint64 by puyapy 5.x
- Removed spurious foreign_assets from ApplicationCreateTxn (not needed for uint64 args)
- Windows puyapy workaround: set VIRTUAL_ENV to resolve python3 correctly

### Deployed
- TestNet App ID: 771550982
- App Address: HD4UT6SCBWGBHR3YUFCLLHZEMD3EVGOONUIFHUAQA4G3D2KIU7FX5QWQVE
- Mock USDC ASA: 771550951

