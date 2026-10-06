use std::fs;
use std::process::Command;

#[test]
fn media_decryption_keeps_derived_and_detected_keys_out_of_diagnostics() {
    let temp = tempfile::tempdir().unwrap();
    let account = temp.path().join("wxid_synthetic_ab12");
    let config = account.join("app_data/radium/ilink/ab12/kvcomm");
    fs::create_dir_all(&config).unwrap();
    fs::write(config.join("config.ini"), "last_uin=MTIzNDU2Nzg5MA==\n").unwrap();
    let derived = wx_media::derive_v2_key_from_dir(&account).unwrap();
    let preview = String::from_utf8_lossy(&derived[..8]);
    let images = temp.path().join("images");
    fs::create_dir(&images).unwrap();
    let plain = [0xff, 0xd8, 0xff, 0xe0, 0, 0, 0xff, 0xd9];
    let encrypted: Vec<_> = plain.iter().map(|b| b ^ 0xa5).collect();
    let input = images.join("sample.dat");
    fs::write(&input, &encrypted).unwrap();
    fs::write(images.join("sample_t.dat"), &encrypted).unwrap();

    for source in [&input, &images] {
        let output = temp.path().join(if source == &input {
            "single.jpg"
        } else {
            "batch"
        });
        let result = Command::new(env!("CARGO_BIN_EXE_wx-cli"))
            .args(["media", "decrypt-dat"])
            .arg(source)
            .arg("--data-dir")
            .arg(&account)
            .arg("--output")
            .arg(&output)
            .output()
            .unwrap();
        assert!(result.status.success(), "synthetic image decoding failed");
        let diagnostics = String::from_utf8_lossy(&result.stderr);
        assert!(diagnostics.contains("Derived V2 image key"));
        assert!(diagnostics.contains("Auto-detected XOR key"));
        assert!(!diagnostics.contains(preview.as_ref()));
        assert!(!diagnostics.contains("0xa5"));
        let image = if source == &input {
            output
        } else {
            output.join("sample.jpg")
        };
        assert_eq!(fs::read(image).unwrap(), plain);
    }
}
