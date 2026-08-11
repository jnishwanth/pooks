{
  lib,
  python3Packages,
}:

# Tracks nixpkgs' DEFAULT python3 on purpose. Do not pin a version here.
#
# Hydra only builds the default Python package set at scale, so pinning to a
# non-default set means every dependency is a cache miss and gets built from
# source. That is not merely slow: building fastapi from source pulls its
# test-only closure (inline-snapshot -> isort -> pylama -> vulture -> pint ->
# uncertainties -> scipy), and a flaky test anywhere in that tree fails the
# whole nixos-rebuild. This exact chain died on a 2e-09 tolerance violation in
# scipy's own suite while the package was pinned to python312Packages.
#
# Deployment parity comes from the nixpkgs revision, not from a version number.
python3Packages.buildPythonApplication {
  pname = "pooks";
  version = "0.1.0";
  pyproject = true;

  src = lib.cleanSource ../.;

  build-system = [ python3Packages.hatchling ];

  dependencies = with python3Packages; [
    httpx
    pydantic
    rapidfuzz
    selectolax
    fastapi
    uvicorn
    jinja2
    apscheduler
    python-telegram-bot
    python-dotenv
    tenacity
  ];

  # The Jinja templates and the SQL schema are data files loaded at runtime by
  # path, not imports, so they have to be told to come along.
  postInstall = ''
    site=$out/${python3Packages.python.sitePackages}/pooks
    install -Dm444 src/pooks/db/schema.sql $site/db/schema.sql
    mkdir -p $site/serve/templates
    install -Dm444 src/pooks/serve/templates/*.html $site/serve/templates/
  '';

  # pyproject.toml pins minimum versions; nixpkgs is generally ahead, and
  # letting the build fail on a lower bound helps nobody.
  pythonRelaxDeps = true;

  nativeCheckInputs = with python3Packages; [
    pytestCheckHook
    pytest-asyncio
    respx
  ];

  # The tests call load_config(), which resolves config.toml relative to the
  # package. During checkPhase the package is imported from the install path,
  # where there is no config.toml — so point it at the one in the source tree
  # and keep any database writes out of the store.
  preCheck = ''
    export POOKS_CONFIG=$PWD/config.toml
    export POOKS_DATA_DIR=$(mktemp -d)
    export POOKS_ENV_FILE=/nonexistent
  '';

  # Every test is offline (fixtures and fakes), so the suite is a genuine
  # build-time check rather than decoration.
  pytestFlags = [ "-q" ];

  meta = {
    description = "Ranked new-arrival pipeline for oldbookdepot.in";
    homepage = "https://github.com/jnishwanth/pooks";
    mainProgram = "pooks";
    license = lib.licenses.mit;
    platforms = lib.platforms.unix;
  };
}
