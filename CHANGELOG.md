# Changelog

All notable changes to Remote AI Bridge are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses semantic versioning for release tags.

## [0.4.0] - 2026-09-26

### Added

- Vue Web management UI with Dashboard, Connections, Profile Detail, and structured application states.
- Managed Setup Wizard for Host Key preparation and confirmation, proxy discovery, one-time password bootstrap, remote-port selection, and Managed Profile creation.
- Web runtime Connect, Disconnect, bounded action polling, and Doctor reporting.
- Safe Profile update flows and explicit Managed Profile credential-cleanup restrictions.

### Changed

- Unified CLI, FastAPI, and Web operations over the existing application service layer.
- Updated the Web interface for coherent setup, runtime, diagnostic, and Profile-management workflows.
- Restricted Python package discovery to the RAB `app` package after adding the top-level Web project.

### Security

- Preserved explicit SSH Host Key fingerprint confirmation before password authentication.
- Kept bootstrap passwords out of persistent Profile and Runtime state, API responses, and intentional logs.
- Preserved loopback-only API serving and remote forwarding defaults.
- Preserved process-ownership validation, stale-session evidence, and unknown-process protections.

### Testing

- Added automated frontend tests for setup, runtime controls, diagnostics, Profile mutation, error handling, and cross-page synchronization.
- Completed real-browser and real-host end-to-end validation of Managed Setup, Connect, READY, Doctor, Disconnect, and STOPPED workflows.
