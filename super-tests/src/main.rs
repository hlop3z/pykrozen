//! PyKrozen Test Suite
//!
//! Comprehensive testing tool for the pykrozen HTTP/WebSocket server library.
//! Tests all endpoints, performs stress testing, and measures requests per second (RPS).

mod config;
mod http_tests;
mod stats;
mod stress;
mod websocket_tests;

use std::process::{Child, Command, Stdio};
use std::time::Duration;

use clap::{Parser, Subcommand};
use colored::Colorize;
use config::Config;

#[derive(Parser)]
#[command(name = "pykrozen-tester")]
#[command(about = "Comprehensive testing tool for pykrozen HTTP/WebSocket server", long_about = None)]
struct Cli {
    #[command(subcommand)]
    command: Commands,

    /// HTTP server host
    #[arg(long, default_value = "127.0.0.1")]
    host: String,

    /// HTTP server port
    #[arg(long, default_value = "8000")]
    http_port: u16,

    /// WebSocket server port
    #[arg(long, default_value = "8765")]
    ws_port: u16,
}

#[derive(Subcommand)]
enum Commands {
    /// Run all tests (GET, POST, WebSocket, stress)
    All {
        /// Number of concurrent connections for stress test
        #[arg(short, long, default_value = "100")]
        concurrency: usize,

        /// Duration of stress test in seconds
        #[arg(short, long, default_value = "10")]
        duration: u64,

        /// Custom endpoints to test (comma-separated paths)
        #[arg(short, long)]
        endpoints: Option<String>,
    },
    /// Test HTTP GET endpoints
    Get {
        /// Specific paths to test (comma-separated)
        #[arg(short, long)]
        paths: Option<String>,
    },
    /// Test HTTP POST endpoints
    Post {
        /// Specific paths to test (comma-separated)
        #[arg(short, long)]
        paths: Option<String>,
    },
    /// Test WebSocket connections
    Websocket {
        /// Number of concurrent WebSocket connections
        #[arg(short, long, default_value = "10")]
        connections: usize,

        /// Number of messages per connection
        #[arg(short, long, default_value = "100")]
        messages: usize,
    },
    /// Run stress tests and measure RPS
    Stress {
        /// Number of concurrent connections
        #[arg(short, long, default_value = "100")]
        concurrency: usize,

        /// Duration of stress test in seconds
        #[arg(short, long, default_value = "10")]
        duration: u64,

        /// Test type: http-get, http-post, websocket, or mixed
        #[arg(short, long, default_value = "mixed")]
        test_type: String,

        /// Target endpoint for HTTP tests
        #[arg(short, long, default_value = "/")]
        endpoint: String,
    },
    /// Quick health check
    Health,
    /// Measure peak RPS at different concurrency levels
    Peak {
        /// Target endpoint for testing
        #[arg(short, long, default_value = "/")]
        endpoint: String,
    },
    /// Auto-start server, run all tests, and cleanup
    Auto {
        /// Number of concurrent connections for stress test
        #[arg(short, long, default_value = "50")]
        concurrency: usize,

        /// Duration of stress test in seconds
        #[arg(short, long, default_value = "5")]
        duration: u64,

        /// Path to Python executable
        #[arg(long, default_value = "python")]
        python: String,
    },
}

fn print_banner() {
    println!(
        "{}",
        r#"
╔═══════════════════════════════════════════════════════════════╗
║                  PyKrozen Test Suite v0.1.0                   ║
║          HTTP & WebSocket Endpoint Testing Tool               ║
╚═══════════════════════════════════════════════════════════════╝
"#
        .cyan()
        .bold()
    );
}

#[tokio::main]
async fn main() {
    let cli = Cli::parse();
    print_banner();

    let config = Config {
        host: cli.host.clone(),
        http_port: cli.http_port,
        ws_port: cli.ws_port,
    };

    println!(
        "{} HTTP: {}:{} | WebSocket: {}:{}",
        "→ Target:".blue().bold(),
        config.host,
        config.http_port,
        config.host,
        config.ws_port
    );
    println!();

    match cli.command {
        Commands::All {
            concurrency,
            duration,
            endpoints,
        } => {
            run_all_tests(&config, concurrency, duration, endpoints).await;
        }
        Commands::Get { paths } => {
            let paths = parse_paths(paths, vec!["/"]);
            http_tests::run_get_tests(&config, &paths).await;
        }
        Commands::Post { paths } => {
            let paths = parse_paths(paths, vec!["/"]);
            http_tests::run_post_tests(&config, &paths).await;
        }
        Commands::Websocket {
            connections,
            messages,
        } => {
            websocket_tests::run_websocket_tests(&config, connections, messages).await;
        }
        Commands::Stress {
            concurrency,
            duration,
            test_type,
            endpoint,
        } => {
            stress::run_stress_test(&config, concurrency, duration, &test_type, &endpoint).await;
        }
        Commands::Health => {
            run_health_check(&config).await;
        }
        Commands::Peak { endpoint } => {
            stress::run_peak_rps_test(&config, &endpoint).await;
        }
        Commands::Auto {
            concurrency,
            duration,
            python,
        } => {
            run_auto_test(&config, concurrency, duration, &python).await;
        }
    }
}

/// Start the Python server process.
fn start_server(python: &str, host: &str, http_port: u16, ws_port: u16) -> std::io::Result<Child> {
    let server_script = std::env::current_dir()?.join("server.py");

    println!(
        "  {} Starting server with {}...",
        "→".blue(),
        server_script.display()
    );

    Command::new(python)
        .arg(&server_script)
        .env("HOST", host)
        .env("HTTP_PORT", http_port.to_string())
        .env("WS_PORT", ws_port.to_string())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
}

/// Stop the server process and cleanup.
fn stop_server(mut child: Child) {
    println!("  {} Stopping server (PID: {})...", "→".blue(), child.id());

    #[cfg(windows)]
    {
        // On Windows, use taskkill to terminate the process tree
        let _ = Command::new("taskkill")
            .args(["/PID", &child.id().to_string(), "/T", "/F"])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status();
    }

    #[cfg(not(windows))]
    {
        let _ = child.kill();
    }

    let _ = child.wait();
    println!("  {} Server stopped", "✓".green());
}

/// Wait for the server to be ready.
async fn wait_for_server(config: &Config, timeout_secs: u64) -> bool {
    let http_url = format!("http://{}:{}/", config.host, config.http_port);
    let start = std::time::Instant::now();
    let timeout = Duration::from_secs(timeout_secs);

    print!("  {} Waiting for server to start", "→".blue());

    while start.elapsed() < timeout {
        print!(".");
        if reqwest::get(&http_url).await.is_ok() {
            println!(" {}", "ready!".green());
            return true;
        }
        tokio::time::sleep(Duration::from_millis(500)).await;
    }

    println!(" {}", "timeout!".red());
    false
}

/// Run auto test: start server, run tests, cleanup.
async fn run_auto_test(config: &Config, concurrency: usize, duration: u64, python: &str) {
    println!("{}", "═══ Auto Test Mode ═══".yellow().bold());
    println!();

    // Start the server
    let server = match start_server(python, &config.host, config.http_port, config.ws_port) {
        Ok(child) => {
            println!(
                "  {} Server process started (PID: {})",
                "✓".green(),
                child.id()
            );
            child
        }
        Err(e) => {
            println!("  {} Failed to start server: {}", "✗".red(), e);
            println!(
                "  {} Make sure '{}' is in PATH and server.py exists",
                "!".yellow(),
                python
            );
            return;
        }
    };

    // Wait for server to be ready
    if !wait_for_server(config, 10).await {
        println!("  {} Server failed to start in time", "✗".red());
        stop_server(server);
        return;
    }

    println!();

    // Run all tests
    run_all_tests(config, concurrency, duration, None).await;

    println!();
    println!("{}", "═══ Cleanup ═══".yellow().bold());

    // Stop the server
    stop_server(server);

    println!();
    println!("{}", "═══ Auto Test Complete ═══".green().bold());
}

fn parse_paths(paths: Option<String>, default: Vec<&str>) -> Vec<String> {
    paths
        .map(|p| p.split(',').map(|s| s.trim().to_string()).collect())
        .unwrap_or_else(|| default.into_iter().map(String::from).collect())
}

async fn run_health_check(config: &Config) {
    println!("{}", "═══ Health Check ═══".yellow().bold());

    // Check HTTP
    let http_url = format!("http://{}:{}/", config.host, config.http_port);
    print!("  {} HTTP Server... ", "Checking".blue());

    match reqwest::get(&http_url).await {
        Ok(resp) => {
            if resp.status().is_success() || resp.status().as_u16() == 200 {
                println!("{} ({})", "OK".green().bold(), resp.status());
            } else {
                println!(
                    "{} ({})",
                    "WARNING".yellow().bold(),
                    resp.status()
                );
            }
        }
        Err(e) => {
            println!("{} - {}", "FAILED".red().bold(), e);
        }
    }

    // Check WebSocket
    let ws_url = format!("ws://{}:{}/", config.host, config.ws_port);
    print!("  {} WebSocket Server... ", "Checking".blue());

    match tokio_tungstenite::connect_async(&ws_url).await {
        Ok((mut ws, _)) => {
            let _ = futures_util::SinkExt::close(&mut ws).await;
            println!("{}", "OK".green().bold());
        }
        Err(e) => {
            println!("{} - {}", "FAILED".red().bold(), e);
        }
    }

    println!();
}

async fn run_all_tests(config: &Config, concurrency: usize, duration: u64, endpoints: Option<String>) {
    let paths = parse_paths(endpoints, vec!["/"]);

    // Health check first
    run_health_check(config).await;

    // GET tests
    println!("{}", "═══ HTTP GET Tests ═══".yellow().bold());
    http_tests::run_get_tests(config, &paths).await;
    println!();

    // POST tests
    println!("{}", "═══ HTTP POST Tests ═══".yellow().bold());
    http_tests::run_post_tests(config, &paths).await;
    println!();

    // WebSocket tests
    println!("{}", "═══ WebSocket Tests ═══".yellow().bold());
    websocket_tests::run_websocket_tests(config, 10, 50).await;
    println!();

    // Stress tests
    println!("{}", "═══ Stress Tests ═══".yellow().bold());

    println!("\n{}", "  HTTP GET Stress Test".cyan());
    stress::run_stress_test(config, concurrency, duration, "http-get", "/").await;

    println!("\n{}", "  HTTP POST Stress Test".cyan());
    stress::run_stress_test(config, concurrency, duration, "http-post", "/").await;

    println!("\n{}", "  WebSocket Stress Test".cyan());
    stress::run_stress_test(config, concurrency, duration, "websocket", "/").await;

    println!("\n{}", "  Mixed Stress Test".cyan());
    stress::run_stress_test(config, concurrency, duration, "mixed", "/").await;

    println!();
    println!("{}", "═══ All Tests Complete ═══".green().bold());
}
