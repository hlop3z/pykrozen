//! Configuration module for the test suite.

/// Configuration for connecting to the pykrozen server.
#[derive(Clone, Debug)]
pub struct Config {
    /// Server host address
    pub host: String,
    /// HTTP server port
    pub http_port: u16,
    /// WebSocket server port
    pub ws_port: u16,
}

impl Config {
    /// Get the full HTTP base URL.
    pub fn http_url(&self) -> String {
        format!("http://{}:{}", self.host, self.http_port)
    }

    /// Get the full WebSocket base URL.
    pub fn ws_url(&self) -> String {
        format!("ws://{}:{}", self.host, self.ws_port)
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
