# Contributing to Brew Update Manager

Thanks for helping improve the project 🙌

## Development prerequisites

- macOS
- Homebrew installed
- Python 3.14
- Node.js + npm

## Local setup

1. Clone the repository.
2. Install dependencies:
   - `npm install`
3. Start the desktop app:
   - `npm run start-app`

## Packaging (local)

- Build a local macOS installer artifact:
  - `npm run build`

Output files are created under `dist/`.

## Pull request flow

1. Create a feature branch.
2. Keep changes focused and small.
3. Verify JavaScript syntax and Python syntax locally before opening PR.
4. Open a PR with:
   - What changed
   - Why it changed
   - How you tested it

## Reporting bugs

Please use GitHub Issues and include:

- macOS version
- Python version (`python3 --version`)
- Steps to reproduce
- Relevant logs from `data/logs/`