%global app_module mirror_mirage

Name:           mirror-mirage
Version:        0.2.0
Release:        1%{?dist}
Summary:        Inotify-driven CDN cache-flush daemon for filesystem mirrors

License:        GPL-3.0-or-later
URL:            https://github.com/AlmaLinux/mirror-mirage
Source:         %{url}/archive/v%{version}/%{name}-%{version}.tar.gz

BuildArch:      noarch

BuildRequires:  python3-devel
BuildRequires:  systemd-rpm-macros

# %%sysusers_create_compat needs shadow-utils on older systems.
%{?sysusers_requires_compat}

# The daemon itself uses Python's stdlib sqlite3 module (linked against
# libsqlite3 and pulled in transitively via python3-libs) — no hard
# Requires needed. The sqlite CLI is recommended because the operations
# runbook uses it to inspect the persistent queue at
# /var/lib/mirror-mirage/queue.db. Recommends, not Requires, so minimal
# installs that don't need queue introspection stay lean.
Recommends:     sqlite

%description
Mirror Mirage watches filesystem mirror trees with inotify and dispatches
CDN cache purges to Fastly, AWS CloudFront, and GCP Cloud CDN whenever
the underlying files change. Bursts of inotify events coalesce per CDN
via a quiet-window debounce plus size and age caps, so a 50,000-file
rsync push produces a handful of bulk purges instead of one API call
per file. Failed purges are retried with exponential backoff from a
SQLite-backed persistent queue that survives daemon restarts and
transient CDN outages. Rsync --delay-updates staging directories are
ignored by default.

%prep
%autosetup -n %{name}-%{version}

%generate_buildrequires
# -x test pulls in pytest, pytest-asyncio, and respx for %%check.
%pyproject_buildrequires -x test

%build
%pyproject_wheel

%install
%pyproject_install
%pyproject_save_files %{app_module}

install -D -m 0644 packaging/%{name}.service  \
    %{buildroot}%{_unitdir}/%{name}.service
install -D -m 0644 packaging/%{name}.sysusers \
    %{buildroot}%{_sysusersdir}/%{name}.conf
install -D -m 0644 packaging/%{name}.tmpfiles \
    %{buildroot}%{_tmpfilesdir}/%{name}.conf
install -d -m 0755 %{buildroot}%{_sysconfdir}/%{name}

%check
%pyproject_check_import
%pytest

%pre
%sysusers_create_compat %{_sysusersdir}/%{name}.conf

%post
%systemd_post %{name}.service
%tmpfiles_create %{_tmpfilesdir}/%{name}.conf

%preun
%systemd_preun %{name}.service

%postun
%systemd_postun_with_restart %{name}.service

%files -f %{pyproject_files}
%license LICENSE
%doc README.md
%doc docs/architecture.md
%doc docs/cdn-providers.md
%doc docs/operations.md
%doc packaging/config.example.yaml
%doc packaging/secrets.example.yaml
%{_unitdir}/%{name}.service
%{_sysusersdir}/%{name}.conf
%{_tmpfilesdir}/%{name}.conf
%dir %{_sysconfdir}/%{name}

%changelog
* Mon Nov 16 2026 Jonathan Wright <jonathan@almalinux.org> - 0.2.0-1
- Update to 0.2.0
- Purge symlink alias URLs (10 -> 10.3, -latest- names) alongside the
  real path; new purge_aliases and max_aliases_per_event settings
- Fastly: optional edge_aliases sync of an edge dictionary that a VCL
  snippet uses to rewrite alias URLs; --edge-aliases show|diff to preview
- Purge the link and its tree when a directory symlink is retargeted
- Retry only the URLs that failed, not the whole batch
- CloudFront: wait out the in-progress invalidation limit
- GCP: keep the invalidation rate under the 500/min quota

* Thu May 28 2026 Jonathan Wright <jonathan@almalinux.org> - 0.1.0-1
- Initial package.
