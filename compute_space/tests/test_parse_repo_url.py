"""Tests for parse_repo_url handling of PAT/credential URLs.

parse_repo_url must correctly handle URLs with embedded credentials
(e.g. oauth2:TOKEN@gitlab.com/user/repo) by recognizing that 'oauth2'
is not a real URL scheme and prepending https://.
"""

from compute_space.core.apps import parse_repo_url


class TestParseRepoUrlCredentialUrls:
    """URLs with credentials like oauth2:TOKEN@host should get https:// prepended."""

    def test_gitlab_pat_without_scheme(self):
        """oauth2:TOKEN@gitlab.com/user/repo.git should become https:// URL."""
        url, ref = parse_repo_url("oauth2:glpat-xxxx@gitlab.com/user/repo.git")
        assert ref is None
        assert url == "https://oauth2:glpat-xxxx@gitlab.com/user/repo.git"

    def test_gitlab_pat_with_https_scheme(self):
        """https://oauth2:TOKEN@gitlab.com/... should be left alone."""
        url, ref = parse_repo_url("https://oauth2:glpat-xxxx@gitlab.com/user/repo.git")
        assert ref is None
        assert url == "https://oauth2:glpat-xxxx@gitlab.com/user/repo.git"

    def test_generic_user_pass_without_scheme(self):
        """user:pass@host/repo should get https:// prepended."""
        url, ref = parse_repo_url("user:pass@github.com/user/repo.git")
        assert ref is None
        assert url == "https://user:pass@github.com/user/repo.git"


class TestParseRepoUrlKnownSchemes:
    """Known schemes (http, https, ssh, git, file) should be preserved."""

    def test_https_url(self):
        url, ref = parse_repo_url("https://github.com/user/repo.git")
        assert url == "https://github.com/user/repo.git"
        assert ref is None

    def test_http_url(self):
        url, ref = parse_repo_url("http://github.com/user/repo.git")
        assert url == "http://github.com/user/repo.git"
        assert ref is None

    def test_ssh_url(self):
        url, ref = parse_repo_url("ssh://git@github.com/user/repo.git")
        assert url == "ssh://git@github.com/user/repo.git"
        assert ref is None

    def test_git_url(self):
        url, ref = parse_repo_url("git://github.com/user/repo.git")
        assert url == "git://github.com/user/repo.git"
        assert ref is None

    def test_file_url(self):
        url, ref = parse_repo_url("file:///home/user/repo")
        assert url == "file:///home/user/repo"
        assert ref is None


class TestParseRepoUrlBareHostname:
    """Bare hostnames without scheme should get https:// prepended."""

    def test_bare_hostname(self):
        url, ref = parse_repo_url("github.com/user/repo.git")
        assert url == "https://github.com/user/repo.git"
        assert ref is None


class TestParseRepoUrlRefSuffix:
    """@ref suffix in the path should still be parsed correctly."""

    def test_https_with_ref(self):
        url, ref = parse_repo_url("https://github.com/user/repo.git@main")
        assert url == "https://github.com/user/repo.git"
        assert ref == "main"

    def test_pat_url_with_ref(self):
        """PAT URL with @ref at the end — ref should be parsed from path, not from credentials."""
        url, ref = parse_repo_url("https://oauth2:glpat-xxxx@gitlab.com/user/repo.git@v2.0")
        assert url == "https://oauth2:glpat-xxxx@gitlab.com/user/repo.git"
        assert ref == "v2.0"
