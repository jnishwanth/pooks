{
  lib,
  python312Packages,
}:

python312Packages.buildPythonApplication {
  pname = "pooks";
  version = "0.1.0";
  pyproject = true;

  src = lib.cleanSource ../.;

  build-system = [ python312Packages.hatchling ];

  dependencies = with python312Packages; [
    httpx
    pydantic
    litellm
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
    site=$out/${python312Packages.python.sitePackages}/pooks
    install -Dm444 src/pooks/db/schema.sql $site/db/schema.sql
    mkdir -p $site/serve/templates
    install -Dm444 src/pooks/serve/templates/*.html $site/serve/templates/
  '';

  # pyproject.toml pins minimum versions; nixpkgs is generally ahead, and
  # letting the build fail on a lower bound helps nobody.
  pythonRelaxDeps = true;

  nativeCheckInputs = with python312Packages; [
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
