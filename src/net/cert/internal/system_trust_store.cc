// Copyright 2017 The Chromium Authors
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

#include "net/cert/internal/system_trust_store.h"

#include <array>
#include <memory>
#include <optional>
#include <vector>

#include "base/environment.h"
#include "base/files/file_enumerator.h"
#include "base/files/file_path.h"
#include "base/files/file_util.h"
#include "base/logging.h"
#include "base/memory/ptr_util.h"
#include "base/no_destructor.h"
#include "base/strings/string_split.h"
#include "base/task/task_traits.h"
#include "base/task/thread_pool.h"
#include "base/threading/thread_restrictions.h"
#include "build/build_config.h"
#include "crypto/crypto_buildflags.h"
#include "net/cert/x509_certificate.h"
#include "net/cert/x509_util.h"
#include "third_party/boringssl/src/pki/cert_errors.h"
#include "third_party/boringssl/src/pki/parsed_certificate.h"
#include "third_party/boringssl/src/pki/trust_store_collection.h"
#include "third_party/boringssl/src/pki/trust_store_in_memory.h"

#if BUILDFLAG(USE_NSS_CERTS)
#include "net/cert/internal/trust_store_nss.h"
#elif BUILDFLAG(IS_MAC)
#include <Security/Security.h>

#include "net/base/features.h"
#include "net/cert/internal/trust_store_mac.h"
#include "net/cert/x509_util_apple.h"
#elif BUILDFLAG(IS_FUCHSIA)
#include "base/lazy_instance.h"
#include "third_party/boringssl/src/include/openssl/pool.h"
#elif BUILDFLAG(IS_WIN)
#include "net/cert/internal/trust_store_win.h"
#elif BUILDFLAG(IS_LINUX) || BUILDFLAG(IS_ANDROID)
#include "base/lazy_instance.h"
#endif

#if BUILDFLAG(CHROME_ROOT_STORE_SUPPORTED)
#include "net/cert/internal/trust_store_chrome.h"
#endif  // CHROME_ROOT_STORE_SUPPORTED

#if BUILDFLAG(IS_CHROMEOS)
#include "base/system/sys_info.h"
#endif

namespace net {

#if BUILDFLAG(IS_CHROMEOS)
namespace internal {
class PemFileCertStore {
 public:
  explicit PemFileCertStore(std::string_view file_name) {
    // This will block on the cert verifier service thread, so the effect will
    // just be to block any cert verifications (interactions with the cert
    // verifier service are async mojo calls, so it shouldn't block the browser
    // UI). There would be no benefit to moving this to a worker thread, since
    // all cert verifications would still need to block on loading of the roots
    // to complete.
    base::ScopedAllowBlocking allow_blocking;
    std::optional<std::vector<uint8_t>> certs_file =
        base::ReadFileToBytes(base::FilePath(file_name));
    if (!certs_file) {
      return;
    }

    trust_store_ = std::make_unique<bssl::TrustStoreInMemory>();

    CertificateList certs = X509Certificate::CreateCertificateListFromBytes(
        *certs_file, X509Certificate::FORMAT_AUTO);

    for (const auto& cert : certs) {
      bssl::CertErrors errors;
      auto parsed = bssl::ParsedCertificate::Create(
          bssl::UpRef(cert->cert_buffer()),
          x509_util::DefaultParseCertificateOptions(), &errors);
      if (!parsed) {
        LOG(ERROR) << file_name << ": " << errors.ToDebugString();
        continue;
      }
      trust_store_->AddTrustAnchor(std::move(parsed));
    }
  }

  bssl::TrustStoreInMemory* trust_store() { return trust_store_.get(); }

 private:
  std::unique_ptr<bssl::TrustStoreInMemory> trust_store_;
};
}  // namespace internal

namespace {

// On ChromeOS look for a PEM file of root CA certs to trust which may be
// present on test images.
bssl::TrustStoreInMemory* GetChromeOSTestTrustStore() {
  constexpr char kCrosTestRootCertsFile[] = "/etc/fake_root_ca_certs.pem";
  static base::NoDestructor<internal::PemFileCertStore> cros_test_roots{
      kCrosTestRootCertsFile};
  return cros_test_roots->trust_store();
}

}  // namespace
#endif

#if BUILDFLAG(CHROME_ROOT_STORE_SUPPORTED)
class SystemTrustStoreChromeWithUnOwnedSystemStore : public SystemTrustStore {
 public:
  // Creates a SystemTrustStore that gets publicly trusted roots from
  // |trust_store_chrome| and local trust settings from |trust_store_system|,
  // if non-null. Does not take ownership of |trust_store_system|, which must
  // outlive this object.
  explicit SystemTrustStoreChromeWithUnOwnedSystemStore(
      std::unique_ptr<TrustStoreChrome> trust_store_chrome,
      net::PlatformTrustStore* trust_store_system)
      : trust_store_chrome_(std::move(trust_store_chrome)),
        platform_trust_store_(trust_store_system) {
#if BUILDFLAG(IS_CHROMEOS)
    if (GetChromeOSTestTrustStore()) {
      // The fake_root_ca_certs.pem file is only intended for testing purposes,
      // crash if it is present on a ChromeOS device in a non-test image.
      base::SysInfo::CrashIfChromeOSNonTestImage();

      trust_store_collection_.AddTrustStore(GetChromeOSTestTrustStore());
      non_crs_trust_store_collection_.AddTrustStore(
          GetChromeOSTestTrustStore());
    }
#endif
    if (trust_store_system) {
      trust_store_collection_.AddTrustStore(trust_store_system);
      non_crs_trust_store_collection_.AddTrustStore(trust_store_system);
    }

    trust_store_collection_.AddTrustStore(trust_store_chrome_.get());
  }

  bssl::TrustStore* GetTrustStore() override {
    return &trust_store_collection_;
  }

  // IsKnownRoot returns true if the given trust anchor is a standard one (as
  // opposed to a user-installed root)
  bool IsKnownRoot(const bssl::ParsedCertificate* trust_anchor) const override {
    return trust_store_chrome_->Contains(trust_anchor);
  }

  bool IsLocallyTrustedRoot(
      const bssl::ParsedCertificate* trust_anchor) override {
    return non_crs_trust_store_collection_.GetTrust(trust_anchor)
        .IsTrustAnchor();
  }

  int64_t chrome_root_store_version() const override {
    return trust_store_chrome_->version();
  }

  base::span<const ChromeRootCertConstraints> GetChromeRootConstraints(
      const bssl::ParsedCertificate* cert) const override {
    return trust_store_chrome_->GetConstraintsForCert(cert);
  }

  bssl::TrustStore* eutl_trust_store() override {
    return trust_store_chrome_->eutl_trust_store();
  }

  net::PlatformTrustStore* GetPlatformTrustStore() override {
    return platform_trust_store_;
  }

 private:
  std::unique_ptr<TrustStoreChrome> trust_store_chrome_;
  bssl::TrustStoreCollection trust_store_collection_;
  bssl::TrustStoreCollection non_crs_trust_store_collection_;
  net::PlatformTrustStore* platform_trust_store_;
};

std::unique_ptr<SystemTrustStore> CreateChromeOnlySystemTrustStore(
    std::unique_ptr<TrustStoreChrome> chrome_root) {
  return std::make_unique<SystemTrustStoreChromeWithUnOwnedSystemStore>(
      std::move(chrome_root), /*trust_store_system=*/nullptr);
}

class SystemTrustStoreChrome
    : public SystemTrustStoreChromeWithUnOwnedSystemStore {
 public:
  // Creates a SystemTrustStore that gets publicly trusted roots from
  // |trust_store_chrome| and local trust settings from |trust_store_system|.
  explicit SystemTrustStoreChrome(
      std::unique_ptr<TrustStoreChrome> trust_store_chrome,
      std::unique_ptr<net::PlatformTrustStore> trust_store_system)
      : SystemTrustStoreChromeWithUnOwnedSystemStore(
            std::move(trust_store_chrome),
            trust_store_system.get()),
        trust_store_system_(std::move(trust_store_system)) {}

 private:
  std::unique_ptr<net::PlatformTrustStore> trust_store_system_;
};

std::unique_ptr<SystemTrustStore> CreateSystemTrustStoreChromeForTesting(
    std::unique_ptr<TrustStoreChrome> trust_store_chrome,
    std::unique_ptr<net::PlatformTrustStore> trust_store_system) {
  return std::make_unique<SystemTrustStoreChrome>(
      std::move(trust_store_chrome), std::move(trust_store_system));
}
#endif  // CHROME_ROOT_STORE_SUPPORTED

#if BUILDFLAG(USE_NSS_CERTS)

std::unique_ptr<SystemTrustStore> CreateSslSystemTrustStoreChromeRoot(
    std::unique_ptr<TrustStoreChrome> chrome_root) {
  return std::make_unique<SystemTrustStoreChrome>(
      std::move(chrome_root), std::make_unique<TrustStoreNSS>(
                                  TrustStoreNSS::UseTrustFromAllUserSlots()));
}

#elif BUILDFLAG(IS_MAC)

namespace {

TrustStoreMac* GetGlobalTrustStoreMacForCRS() {
  constexpr TrustStoreMac::TrustImplType kDefaultMacTrustImplForCRS =
      TrustStoreMac::TrustImplType::kDomainCacheFullCerts;
  static base::NoDestructor<TrustStoreMac> static_trust_store_mac(
      kSecPolicyAppleSSL, kDefaultMacTrustImplForCRS);
  return static_trust_store_mac.get();
}

void InitializeTrustCacheForCRSOnWorkerThread() {
  GetGlobalTrustStoreMacForCRS()->InitializeTrustCache();
}

}  // namespace

std::unique_ptr<SystemTrustStore> CreateSslSystemTrustStoreChromeRoot(
    std::unique_ptr<TrustStoreChrome> chrome_root) {
  return std::make_unique<SystemTrustStoreChromeWithUnOwnedSystemStore>(
      std::move(chrome_root), GetGlobalTrustStoreMacForCRS());
}

void InitializeTrustStoreMacCache() {
  base::ThreadPool::PostTask(
      FROM_HERE,
      {base::MayBlock(), base::TaskShutdownBehavior::CONTINUE_ON_SHUTDOWN},
      base::BindOnce(&InitializeTrustCacheForCRSOnWorkerThread));
}

#elif BUILDFLAG(IS_FUCHSIA)

namespace {

constexpr char kRootCertsFileFuchsia[] = "/config/ssl/cert.pem";

class FuchsiaSystemCerts {
 public:
  FuchsiaSystemCerts() {
    base::FilePath filename(kRootCertsFileFuchsia);
    std::string certs_file;
    if (!base::ReadFileToString(filename, &certs_file)) {
      LOG(ERROR) << "Can't load root certificates from " << filename;
      return;
    }

    CertificateList certs = X509Certificate::CreateCertificateListFromBytes(
        base::as_byte_span(certs_file), X509Certificate::FORMAT_AUTO);

    for (const auto& cert : certs) {
      bssl::CertErrors errors;
      auto parsed = bssl::ParsedCertificate::Create(
          bssl::UpRef(cert->cert_buffer()),
          x509_util::DefaultParseCertificateOptions(), &errors);
      CHECK(parsed) << errors.ToDebugString();
      system_trust_store_.AddTrustAnchor(std::move(parsed));
    }
  }

  bssl::TrustStoreInMemory* system_trust_store() {
    return &system_trust_store_;
  }

 private:
  bssl::TrustStoreInMemory system_trust_store_;
};

base::LazyInstance<FuchsiaSystemCerts>::Leaky g_root_certs_fuchsia =
    LAZY_INSTANCE_INITIALIZER;

}  // namespace

class SystemTrustStoreFuchsia : public SystemTrustStore {
 public:
  SystemTrustStoreFuchsia() = default;

  bssl::TrustStore* GetTrustStore() override {
    return g_root_certs_fuchsia.Get().system_trust_store();
  }

  bool IsKnownRoot(const bssl::ParsedCertificate* trust_anchor) const override {
    return g_root_certs_fuchsia.Get().system_trust_store()->Contains(
        trust_anchor);
  }
};

std::unique_ptr<SystemTrustStore> CreateSslSystemTrustStore() {
  return std::make_unique<SystemTrustStoreFuchsia>();
}

#elif BUILDFLAG(IS_LINUX) || BUILDFLAG(IS_ANDROID)

namespace {

// Copied from https://golang.org/src/crypto/x509/root_linux.go
// Possible certificate files; stop after finding one.
constexpr std::array<const char*, 6> kStaticRootCertFiles = {
    "/etc/ssl/certs/ca-certificates.crt",  // Debian/Ubuntu/Gentoo etc.
    "/etc/pki/tls/certs/ca-bundle.crt",    // Fedora/RHEL 6
    "/etc/ssl/ca-bundle.pem",              // OpenSUSE
    "/etc/pki/tls/cacert.pem",             // OpenELEC
    "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem",  // CentOS/RHEL 7
    "/etc/ssl/cert.pem",                                  // Alpine Linux
};

// Possible directories with certificate files; stop after successfully
// reading at least one file from a directory.
constexpr std::array<const char*, 3> kStaticRootCertDirs = {
    "/etc/ssl/certs",      // SLES10/SLES11, https://golang.org/issue/12139
    "/etc/pki/tls/certs",  // Fedora/RHEL
    "/system/etc/security/cacerts",  // Android
};

// The environment variable which identifies where to locate the SSL
// certificate file. If set this overrides the system default.
constexpr char kStaticCertFileEnv[] = "SSL_CERT_FILE";

// The environment variable which identifies which directory to check for SSL
// certificate files. If set this overrides the system default. It is a colon
// separated list of directories.
// See https://www.openssl.org/docs/man1.0.2/man1/c_rehash.html.
constexpr char kStaticCertDirsEnv[] = "SSL_CERT_DIR";

class TrustStoreUnix : public PlatformTrustStore {
 public:
  TrustStoreUnix() {
    auto env = base::Environment::Create();
    std::optional<std::string> env_value;
    std::vector<std::string> cert_filenames(kStaticRootCertFiles.begin(),
                                            kStaticRootCertFiles.end());
    env_value = env->GetVar(kStaticCertFileEnv);
    if (env_value.has_value() && !env_value->empty()) {
      cert_filenames = {*env_value};
    }

    bool cert_file_ok = false;
    for (const auto& filename : cert_filenames) {
      std::string file;
      if (!base::ReadFileToString(base::FilePath(filename), &file))
        continue;
      if (AddCertificatesFromBytes(file, trust_store_)) {
        cert_file_ok = true;
        break;
      }
    }

    std::vector<std::string> cert_dirnames(kStaticRootCertDirs.begin(),
                                           kStaticRootCertDirs.end());
    env_value = env->GetVar(kStaticCertDirsEnv);
    if (env_value.has_value() && !env_value->empty()) {
      cert_dirnames = base::SplitString(*env_value, ":", base::TRIM_WHITESPACE,
                                        base::SPLIT_WANT_NONEMPTY);
    }

    bool cert_dir_ok = false;
    for (const auto& dir : cert_dirnames) {
      base::FileEnumerator e(base::FilePath(dir),
                             /*recursive=*/true, base::FileEnumerator::FILES);
      for (auto filename = e.Next(); !filename.empty(); filename = e.Next()) {
        std::string file;
        if (!base::ReadFileToString(filename, &file)) {
          continue;
        }
        if (AddCertificatesFromBytes(file, trust_store_)) {
          cert_dir_ok = true;
        }
      }
      if (cert_dir_ok)
        break;
    }

    if (!cert_file_ok && !cert_dir_ok) {
      LOG(ERROR) << "No CA certificates were found. Try using environment "
                    "variable SSL_CERT_FILE or SSL_CERT_DIR";
    }
  }

  TrustStoreUnix(const TrustStoreUnix&) = delete;
  TrustStoreUnix& operator=(const TrustStoreUnix&) = delete;

  // bssl::CertIssuerSource implementation:
  void SyncGetIssuersOf(const bssl::ParsedCertificate* cert,
                        bssl::ParsedCertificateList* issuers) override {
    trust_store_.SyncGetIssuersOf(cert, issuers);
  }

  // bssl::TrustStore implementation:
  bssl::CertificateTrust GetTrust(
      const bssl::ParsedCertificate* cert) override {
    return trust_store_.GetTrust(cert);
  }

  // net::PlatformTrustStore implementation:
  std::vector<net::PlatformTrustStore::CertWithTrust> GetAllUserAddedCerts()
      override {
    return {};
  }

 private:
  static bool AddCertificatesFromBytes(std::string_view data,
                                       bssl::TrustStoreInMemory& store) {
    auto certs = X509Certificate::CreateCertificateListFromBytes(
        base::as_bytes(base::span(data)), X509Certificate::FORMAT_AUTO);
    bool certs_ok = false;
    for (const auto& cert : certs) {
      bssl::CertErrors errors;
      auto parsed = bssl::ParsedCertificate::Create(
          bssl::UpRef(cert->cert_buffer()),
          x509_util::DefaultParseCertificateOptions(), &errors);
      if (parsed) {
        if (!store.Contains(parsed.get())) {
          store.AddTrustAnchor(parsed);
        }
        certs_ok = true;
      } else {
        LOG(ERROR) << errors.ToDebugString();
      }
    }
    return certs_ok;
  }

  bssl::TrustStoreInMemory trust_store_;
};

}  // namespace

std::unique_ptr<SystemTrustStore> CreateSslSystemTrustStoreChromeRoot(
    std::unique_ptr<TrustStoreChrome> chrome_root) {
  return std::make_unique<SystemTrustStoreChrome>(
      std::move(chrome_root), std::make_unique<TrustStoreUnix>());
}

#elif BUILDFLAG(IS_WIN)

namespace {
TrustStoreWin* GetGlobalTrustStoreWinForCRS() {
  static base::NoDestructor<TrustStoreWin> static_trust_store_win;
  return static_trust_store_win.get();
}

void InitializeTrustStoreForCRSOnWorkerThread() {
  GetGlobalTrustStoreWinForCRS()->InitializeStores();
}
}  // namespace

std::unique_ptr<SystemTrustStore> CreateSslSystemTrustStoreChromeRoot(
    std::unique_ptr<TrustStoreChrome> chrome_root) {
  return std::make_unique<SystemTrustStoreChromeWithUnOwnedSystemStore>(
      std::move(chrome_root), GetGlobalTrustStoreWinForCRS());
}

// We do this in a separate thread as loading the Windows Cert Stores can cause
// quite a bit of I/O. See crbug.com/1399974 for more context.
void InitializeTrustStoreWinSystem() {
  base::ThreadPool::PostTask(
      FROM_HERE,
      {base::MayBlock(), base::TaskShutdownBehavior::CONTINUE_ON_SHUTDOWN},
      base::BindOnce(&InitializeTrustStoreForCRSOnWorkerThread));
}

#elif BUILDFLAG(IS_ANDROID)

#if BUILDFLAG(CHROME_ROOT_STORE_SUPPORTED)

namespace {
TrustStoreAndroid* GetGlobalTrustStoreAndroidForCRS() {
  static base::NoDestructor<TrustStoreAndroid> static_trust_store_android;
  return static_trust_store_android.get();
}

void InitializeTrustStoreForCRSOnWorkerThread() {
  GetGlobalTrustStoreAndroidForCRS()->Initialize();
}
}  // namespace

std::unique_ptr<SystemTrustStore> CreateSslSystemTrustStoreChromeRoot(
    std::unique_ptr<TrustStoreChrome> chrome_root) {
  return std::make_unique<SystemTrustStoreChromeWithUnOwnedSystemStore>(
      std::move(chrome_root), GetGlobalTrustStoreAndroidForCRS());
}

void InitializeTrustStoreAndroid() {
  // Start observing DB change before the Trust Store is initialized so we don't
  // accidentally miss any changes. See https://crrev.com/c/4226436 for context.
  //
  // This call is safe here because we're the only callers of
  // ObserveCertDBChanges on the singleton TrustStoreAndroid.
  GetGlobalTrustStoreAndroidForCRS()->ObserveCertDBChanges();

  base::ThreadPool::PostTask(
      FROM_HERE,
      {base::MayBlock(), base::TaskShutdownBehavior::SKIP_ON_SHUTDOWN},
      base::BindOnce(&InitializeTrustStoreForCRSOnWorkerThread));
}

#else

void InitializeTrustStoreAndroid() {}

#endif  // CHROME_ROOT_STORE_SUPPORTED

#endif

}  // namespace net
