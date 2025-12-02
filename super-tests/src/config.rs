//! Configuration module for the test suite.
//!
//! Supports loading configuration from a TOML file.

use serde::{Deserialize, Serialize};
use std::fs;
use std::path::Path;

/// Configuration for connecting to the pykrozen server.
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(default)]
pub struct Config {
    /// Server host address
    pub host: String,
    /// HTTP server port
    pub http_port: u16,
    /// WebSocket server port
    pub ws_port: u16,
}

/// Test configuration settings.
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(default)]
pub struct TestConfig {
    /// Number of concurrent workers for stress tests
    pub concurrency: usize,
    /// Duration of stress tests in seconds
    pub duration: u64,
    /// Path to Python executable
    pub python: String,
}

/// Complete configuration file structure.
#[derive(Clone, Debug, Serialize, Deserialize, Default)]
#[serde(default)]
pub struct ConfigFile {
    /// Server connection settings
    pub server: Config,
    /// Test settings
    pub test: TestConfig,
}

impl Config {
    /// Get the full HTTP base URL.
    pub fn http_url(&self) -> String {
        format!("http://{}:{}", self.host, self.http_port)
    }

    /// Get the full WebSocket base URL.
    /// Note: aiohttp async server uses /ws path on same port as HTTP.
    pub fn ws_url(&self) -> String {
        format!("ws://{}:{}/ws", self.host, self.ws_port)
    }
}

impl Default for Config {
    fn default() -> Self {
        Self {
            host: "127.0.0.1".to_string(),
            http_port: 8000,
            ws_port: 8765,
        }
    }
}

impl Default for TestConfig {
    fn default() -> Self {
        Self {
            concurrency: 50,
            duration: 5,
            python: "python".to_string(),
        }
    }
}

impl ConfigFile {
    /// Load configuration from a TOML file.
    /// Returns default config if file doesn't exist.
    pub fn load<P: AsRef<Path>>(path: P) -> Self {
        let path = path.as_ref();

        if !path.exists() {
            return Self::default();
        }

        match fs::read_to_string(path) {
            Ok(contents) => match toml::from_str(&contents) {
                Ok(config) => config,
                Err(e) => {
                    eprintln!("Warning: Failed to parse config file: {}", e);
                    eprintln!("Using default configuration.");
                    Self::default()
                }
            },
            Err(e) => {
                eprintln!("Warning: Failed to read config file: {}", e);
                eprintln!("Using default configuration.");
                Self::default()
            }
        }
    }

    /// Load from default config file paths.
    /// Checks in order: ./pykrozen-test.toml, ./config.toml
    pub fn load_default() -> Self {
        let paths = ["pykrozen-test.toml", "config.toml", "../pykrozen-test.toml"];

        for path in paths {
            if Path::new(path).exists() {
                println!("Loading config from: {}", path);
                return Self::load(path);
            }
        }

        Self::default()
    }

    /// Save configuration to a TOML file.
    pub fn save<P: AsRef<Path>>(&self, path: P) -> std::io::Result<()> {
        let contents = toml::to_string_pretty(self).expect("Failed to serialize config");
        fs::write(path, contents)
    }

    /// Generate a default config file.
    pub fn generate_default<P: AsRef<Path>>(path: P) -> std::io::Result<()> {
        Self::default().save(path)
    }
}
