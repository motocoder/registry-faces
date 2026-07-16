from web_scrubber.expand import GitSync

from registry_faces.expand import ADAPTERS_REL, PROJECT_ROOT, build_spec


def test_expand_spec_lands_generated_adapters_through_git():
    spec = build_spec()

    assert isinstance(spec.git, GitSync)
    assert spec.git.repo == PROJECT_ROOT
    assert spec.scrubber_paths == [ADAPTERS_REL]
