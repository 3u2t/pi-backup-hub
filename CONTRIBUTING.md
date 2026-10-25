# Contributing

Bug reports, clearer instructions and pull requests are welcome.

For a bug report, include what you expected, what happened, and the relevant app,
Immich and PostgreSQL versions. Remove passwords, tokens, email addresses, photo
names and personal paths from logs before sharing them. Do not attach your app
data directory, Google client file, sign-in helper or recovery key.

For code changes, keep the pull request focused and explain how you tested it.
Please keep the code free of comments and put explanations in the documentation
or pull request. Documentation is in English; the current dashboard is in German.

## Local checks

Run these in the project directory on Linux or macOS:

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
python -m pytest -q
node --check static/app.js
```

The unit tests use temporary directories and mocked Google responses. They do
not connect to a real account or prove that a complete cloud restore works.

`tests/pi_integration.py` is a separate test for a configured Linux container.
It checks read-only access, takes a real Immich database dump and restores a
test file using a local encrypted repository. It does not upload to Google.
Do not run it against an unconfigured installation.

## License for contributions

You keep the copyright in your contribution. By intentionally submitting it
for inclusion, you offer it under the project's [No-Sale License](LICENSE).
Only submit material you have the right to contribute. There is no copyright
assignment to the maintainer.
