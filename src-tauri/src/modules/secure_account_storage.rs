//! AES-256-GCM envelopes for provider account detail files (#1104).
//!
//! Index/summary files stay plaintext; only per-account detail JSON is encrypted
//! at rest. macOS stores the encryption key in the system Keychain, scoped to
//! this app and data directory. Keychain failures never fall back to a key file.

use aes_gcm::aead::Aead;
use aes_gcm::{Aes256Gcm, KeyInit, Nonce};
use base64::{engine::general_purpose, Engine as _};
use rand::RngCore;
use serde::{de::DeserializeOwned, Deserialize, Serialize};
use std::fs;
use std::path::{Path, PathBuf};

const KEY_FILE: &str = "secure-account-storage.key";
const VERSION: u32 = 1;
const ROTATION_SECONDS: i64 = 30 * 24 * 60 * 60;

#[derive(Debug, Clone, Serialize, Deserialize)]
struct SecureAccountEnvelope {
    version: u32,
    kind: String,
    algorithm: String,
    key_id: String,
    nonce: String,
    ciphertext: String,
    encrypted_at: i64,
}

fn key_path() -> Result<PathBuf, String> {
    Ok(crate::modules::account::get_data_dir()?.join(KEY_FILE))
}

fn parse_key(bytes: &[u8]) -> Result<[u8; 32], String> {
    bytes
        .try_into()
        .map_err(|_| "账号详情加密密钥长度无效".to_string())
}

#[cfg(target_os = "macos")]
fn reject_legacy_key_file(path: &Path) -> Result<(), String> {
    match fs::symlink_metadata(path) {
        Ok(_) => Err(
            "检测到旧版明文账号密钥；此独立版本不会读取、覆盖或自动迁移，请使用独立数据目录"
                .to_string(),
        ),
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(err) => Err(format!("检查旧版账号密钥失败: {}", err)),
    }
}

#[cfg(all(target_os = "macos", not(test)))]
fn load_key(path: &Path, create: bool) -> Result<[u8; 32], String> {
    use security_framework::os::macos::keychain::SecKeychain;
    use sha2::{Digest, Sha256};

    const SERVICE: &str = "io.github.cockpit-accounts.account-storage.v1";
    // Security.framework OSStatus values: missing item and duplicate item.
    const ITEM_NOT_FOUND: i32 = -25300;
    const DUPLICATE_ITEM: i32 = -25299;

    reject_legacy_key_file(path)?;
    let root = path.parent().ok_or("账号详情目录无效")?;
    let root = fs::canonicalize(root).map_err(|e| format!("解析账号详情目录失败: {}", e))?;
    use std::os::unix::ffi::OsStrExt;
    let account = format!("profile-{:x}", Sha256::digest(root.as_os_str().as_bytes()));
    let keychain = SecKeychain::default().map_err(|e| format!("打开系统钥匙串失败: {}", e))?;
    match keychain.find_generic_password(SERVICE, &account) {
        Ok((password, _)) => return parse_key(password.as_ref()),
        Err(err) if err.code() == ITEM_NOT_FOUND && create => {}
        Err(err) => return Err(format!("读取账号详情钥匙串密钥失败: {}", err)),
    }

    let mut key = [0u8; 32];
    rand::thread_rng().fill_bytes(&mut key);
    // Add only: another process may have created the key after our read. Never
    // replace its key, which would make already encrypted accounts unreadable.
    match keychain.add_generic_password(SERVICE, &account, &key) {
        Ok(()) => Ok(key),
        Err(err) if err.code() == DUPLICATE_ITEM => {
            let (password, _) = keychain
                .find_generic_password(SERVICE, &account)
                .map_err(|e| format!("读取并发创建的账号详情钥匙串密钥失败: {}", e))?;
            parse_key(password.as_ref())
        }
        Err(err) => Err(format!("创建账号详情钥匙串密钥失败: {}", err)),
    }
}

#[cfg(all(not(target_os = "macos"), not(test)))]
fn load_key(_path: &Path, _create: bool) -> Result<[u8; 32], String> {
    Err("此安全版本的账号密钥存储仅支持 macOS 系统钥匙串".to_string())
}

// Unit tests use isolated temporary fixtures, never the real system Keychain.
// This file-backed implementation is entirely absent from production builds.
#[cfg(test)]
fn load_key(path: &Path, create: bool) -> Result<[u8; 32], String> {
    use std::io::Write;
    match fs::read_to_string(path) {
        Ok(raw) => {
            let bytes = general_purpose::STANDARD
                .decode(raw.trim())
                .map_err(|e| format!("解析测试账号密钥失败: {}", e))?;
            return parse_key(&bytes);
        }
        Err(err) if err.kind() == std::io::ErrorKind::NotFound && create => {}
        Err(err) => return Err(format!("读取测试账号密钥失败: {}", err)),
    }
    let mut options = fs::OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let mut file = match options.open(path) {
        Ok(file) => file,
        Err(err) if err.kind() == std::io::ErrorKind::AlreadyExists => {
            return load_key(path, false);
        }
        Err(err) => return Err(format!("创建测试账号密钥失败: {}", err)),
    };
    let mut key = [0u8; 32];
    rand::thread_rng().fill_bytes(&mut key);
    file.write_all(general_purpose::STANDARD.encode(key).as_bytes())
        .map_err(|e| format!("写入测试账号密钥失败: {}", e))?;
    Ok(key)
}

fn cipher(create: bool) -> Result<Aes256Gcm, String> {
    Aes256Gcm::new_from_slice(&load_key(&key_path()?, create)?)
        .map_err(|e| format!("初始化账号详情加密失败: {}", e))
}

/// Recovery must not create a missing key, rotate credentials or restore a
/// backup over the source file. `key_path` identifies the caller's account
/// directory; production macOS resolves its Keychain item without reading that
/// file, so background work cannot drift to another profile.
pub(crate) fn read_account_file_readonly<T: DeserializeOwned>(
    path: &Path,
    key_path: &Path,
) -> Result<T, String> {
    let content = fs::read_to_string(path).map_err(|e| format!("读取账号详情失败: {}", e))?;
    if let Ok(envelope) = serde_json::from_str::<SecureAccountEnvelope>(&content) {
        let key = load_key(key_path, false)?;
        let cipher = Aes256Gcm::new_from_slice(&key)
            .map_err(|e| format!("初始化账号详情加密失败: {}", e))?;
        return decrypt_envelope(&envelope, cipher);
    }
    serde_json::from_str(&content).map_err(|e| format!("解析账号详情失败: {}", e))
}

fn decrypt_envelope<T: DeserializeOwned>(
    envelope: &SecureAccountEnvelope,
    cipher: Aes256Gcm,
) -> Result<T, String> {
    if envelope.version != VERSION || envelope.algorithm != "AES-256-GCM" {
        return Err("账号详情加密版本不受支持".to_string());
    }
    let nonce = general_purpose::STANDARD
        .decode(envelope.nonce.trim())
        .map_err(|e| format!("解析账号详情 nonce 失败: {}", e))?;
    if nonce.len() != 12 {
        return Err("账号详情 nonce 长度无效".to_string());
    }
    let ciphertext = general_purpose::STANDARD
        .decode(envelope.ciphertext.trim())
        .map_err(|e| format!("解析账号详情密文失败: {}", e))?;
    let plaintext = cipher
        .decrypt(Nonce::from_slice(&nonce), ciphertext.as_ref())
        .map_err(|e| format!("解密账号详情失败: {:?}", e))?;
    serde_json::from_slice(&plaintext).map_err(|e| format!("解析账号详情明文失败: {}", e))
}

pub fn serialize_account_file<T: Serialize>(kind: &str, account: &T) -> Result<String, String> {
    let plaintext =
        serde_json::to_vec(account).map_err(|e| format!("序列化账号详情失败: {}", e))?;
    let mut nonce_bytes = [0u8; 12];
    rand::thread_rng().fill_bytes(&mut nonce_bytes);
    let ciphertext = cipher(true)?
        .encrypt(Nonce::from_slice(&nonce_bytes), plaintext.as_ref())
        .map_err(|e| format!("加密账号详情失败: {:?}", e))?;
    let envelope = SecureAccountEnvelope {
        version: VERSION,
        kind: kind.to_string(),
        algorithm: "AES-256-GCM".to_string(),
        key_id: "local-secure-account-storage-v1".to_string(),
        nonce: general_purpose::STANDARD.encode(nonce_bytes),
        ciphertext: general_purpose::STANDARD.encode(ciphertext),
        encrypted_at: chrono::Utc::now().timestamp(),
    };
    serde_json::to_string_pretty(&envelope).map_err(|e| format!("序列化账号详情密文失败: {}", e))
}

/// Returns `(account, needs_rewrite)` where `needs_rewrite` is true for legacy
/// plaintext files or envelopes past the rotation window.
pub fn deserialize_account_file<T: DeserializeOwned>(
    path: &Path,
    content: &str,
) -> Result<(T, bool), String> {
    if let Ok(envelope) = serde_json::from_str::<SecureAccountEnvelope>(content) {
        let value = decrypt_envelope(&envelope, cipher(false)?)?;
        let needs_rotation =
            chrono::Utc::now().timestamp() - envelope.encrypted_at > ROTATION_SECONDS;
        return Ok((value, needs_rotation));
    }

    let value = crate::modules::atomic_write::parse_json_with_auto_restore::<T>(path, content)
        .map_err(|e| format!("解析账号详情失败: {}", e))?;
    Ok((value, true))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde::{Deserialize, Serialize};
    use std::sync::Mutex;

    static TEST_LOCK: Mutex<()> = Mutex::new(());

    #[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
    struct DemoAccount {
        id: String,
        secret: String,
    }

    struct TempDir(PathBuf);

    impl TempDir {
        fn new() -> Self {
            let path = std::env::temp_dir().join(format!(
                "secure-account-storage-{}-{}",
                std::process::id(),
                uuid::Uuid::new_v4()
            ));
            fs::create_dir_all(&path).expect("temp dir");
            Self(path)
        }
    }

    impl Drop for TempDir {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    #[test]
    fn missing_read_key_and_invalid_existing_key_are_not_replaced() {
        let dir = TempDir::new();
        let key_path = dir.0.join(KEY_FILE);
        assert!(load_key(&key_path, false).is_err());
        assert!(!key_path.exists(), "reading must not create a key");

        fs::write(&key_path, "invalid-existing-key").unwrap();
        assert!(load_key(&key_path, true).is_err());
        assert_eq!(
            fs::read_to_string(&key_path).unwrap(),
            "invalid-existing-key"
        );
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn macos_rejects_legacy_key_file_without_changing_it() {
        let dir = TempDir::new();
        let key_path = dir.0.join(KEY_FILE);
        assert!(reject_legacy_key_file(&key_path).is_ok());
        fs::write(&key_path, "legacy-key-fixture").unwrap();
        assert!(reject_legacy_key_file(&key_path).is_err());
        assert_eq!(fs::read_to_string(&key_path).unwrap(), "legacy-key-fixture");
    }

    #[test]
    fn roundtrip_encrypt_decrypt_and_legacy_plaintext() {
        let _guard = TEST_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let _lock = crate::modules::test_support::env_lock()
            .lock()
            .unwrap_or_else(|e| e.into_inner());

        let dir = std::env::temp_dir().join(format!(
            "secure-account-storage-test-{}-{}",
            std::process::id(),
            chrono::Utc::now().timestamp_nanos_opt().unwrap_or(0)
        ));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).expect("temp dir");
        std::env::set_var("COCKPIT_TOOLS_TEST_DATA_DIR", &dir);

        let account = DemoAccount {
            id: "a1".into(),
            secret: "tok-secret".into(),
        };
        let path = dir.join("a1.json");
        let enc = serialize_account_file("demo", &account).expect("encrypt");
        assert!(enc.contains("AES-256-GCM"));
        assert!(!enc.contains("tok-secret"));
        fs::write(&path, &enc).expect("write");

        let (decoded, needs_rotation) =
            deserialize_account_file::<DemoAccount>(&path, &enc).expect("decrypt");
        assert_eq!(decoded, account);
        assert!(!needs_rotation);

        let key_path = dir.join(KEY_FILE);
        let saved_key = fs::read(&key_path).unwrap();
        fs::remove_file(&key_path).unwrap();
        assert!(deserialize_account_file::<DemoAccount>(&path, &enc).is_err());
        assert!(read_account_file_readonly::<DemoAccount>(&path, &key_path).is_err());
        assert!(
            !key_path.exists(),
            "decryption must not generate a replacement key"
        );
        assert_eq!(fs::read_to_string(&path).unwrap(), enc);
        fs::write(&key_path, saved_key).unwrap();
        assert_eq!(
            read_account_file_readonly::<DemoAccount>(&path, &key_path).unwrap(),
            account
        );

        let plain = serde_json::to_string_pretty(&account).unwrap();
        let (legacy, needs_migration) =
            deserialize_account_file::<DemoAccount>(&path, &plain).expect("legacy");
        assert_eq!(legacy, account);
        assert!(needs_migration);

        std::env::remove_var("COCKPIT_TOOLS_TEST_DATA_DIR");
        let _ = fs::remove_dir_all(&dir);
    }
}
