//! PyKrozen Test Suite
//!
//! Comprehensive testing tool for the pykrozen HTTP/WebSocket server library.
//! Tests all endpoints, performs stress testing, and measures requests per second (RPS).

mod config;
mod http_tests;
mod report;
mod stats;
mod stress;
mod websocket_tests;

use std::process::{Child, Command, Stdio};
use std::sync::Arc;
use std::time::Duration;

use clap::{Parser, Subcommand};
use colored::Colorize;
use config::{Config, ConfigFile};
use report::MarkdownReport;
use tokio::sync::Mutex;

#[derive(Parser)]
#[command(name = "pykrozen-tester")]
#[command(about = "Comprehensive testing tool for pykrozen HTTP/WebSocket server", long_about = None)]
struct Cli {
    #[command(subcommand)]
    command: Commands,

    /// HTTP server host (overrides config file)
    #[arg(long)]
    host: Option<String>,

    /// HTTP server port (overrides config file)
    #[arg(long)]
    http_port: Option<u16>,

    /// WebSocket server port (overrides config file)
    #[arg(long)]
    ws_port: Option<u16>,

    /// Path to config file
    #[arg(long, short = 'f')]
    config: Option<String>,
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
        /// Number of concurrent connections for stress test (overrides config)
        #[arg(short, long)]
        concurrency: Option<usize>,

        /// Duration of stress test in seconds (overrides config)
        #[arg(short, long)]
        duration: Option<u64>,

        /// Path to Python executable (overrides config)
        #[arg(long)]
        python: Option<String>,
    },
    /// Generate a default config file
    GenConfig {
        /// Output path for config file
        #[arg(short, long, default_value = "pykrozen-test.toml")]
        output: String,
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

    // Load config from file first
    let config_file = match &cli.config {
        Some(path) => {
            println!("{} Loading config from: {}", "→".blue(), path);
            ConfigFile::load(path)
        }
        None => ConfigFile::load_default(),
    };

    // CLI arguments override config file values
    let config = Config {
        host: cli.host.clone().unwrap_or(config_file.server.host.clone()),
        http_port: cli.http_port.unwrap_or(config_file.server.http_port),
        ws_port: cli.ws_port.unwrap_or(config_file.server.ws_port),
    };

    // Handle gen-config before printing target info
    if let Commands::GenConfig { output } = &cli.command {
        println!("{} Generating config file: {}", "→".blue(), output);
        match ConfigFile::generate_default(output) {
            Ok(()) => {
                println!("{} Config file created successfully!", "✓".green());
                println!("\nEdit {} to customize settings.", output);
            }
            Err(e) => {
                println!("{} Failed to create config file: {}", "✗".red(), e);
            }
        }
        return;
    }

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
            let report = Arc::new(Mutex::new(MarkdownReport::new(
                &config.host,
                config.http_port,
                config.ws_port,
            )));
            run_all_tests(
                &config,
                concurrency,
                duration,
                endpoints,
                Some(report.clone()),
            )
            .await;

            // Write report
            let rpt = report.lock().await;
            match rpt.write_default() {
                Ok(filename) => {
                    println!(
                        "\n{} Report saved to: {}",
                        "📄".to_string(),
                        filename.green()
                    );
                }
                Err(e) => {
                    println!("\n{} Failed to write report: {}", "⚠".yellow(), e);
                }
            }
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
            let concurrency = concurrency.unwrap_or(config_file.test.concurrency);
            let duration = duration.unwrap_or(config_file.test.duration);
            let python = python.unwrap_or(config_file.test.python.clone());
            run_auto_test(&config, concurrency, duration, &python).await;
        }
        Commands::GenConfig { .. } => {
            // Already handled above
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

    // Split python command to handle "uv run python" style commands
    let parts: Vec<&str> = python.split_whitespace().collect();
    let (cmd, args) = parts.split_first().ok_or_else(|| {
        std::io::Error::new(std::io::ErrorKind::InvalidInput, "Empty python command")
    })?;

    Command::new(cmd)
        .args(args)
        .arg(&server_script)
        .env("HOST", host)
        .env("HTTP_PORT", http_port.to_string())
        .env("WS_PORT", ws_port.to_string())
        .stdout(Stdio::inherit())
        .stderr(Stdio::inherit())
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

    // Create report
    let report = Arc::new(Mutex::new(MarkdownReport::new(
        &config.host,
        config.http_port,
        config.ws_port,
    )));

    // Run all tests
    run_all_tests(config, concurrency, duration, None, Some(report.clone())).await;

    // Write report
    let rpt = report.lock().await;
    match rpt.write_default() {
        Ok(filename) => {
            println!(
                "\n{} Report saved to: {}",
                "📄".to_string(),
                filename.green()
            );
        }
        Err(e) => {
            println!("\n{} Failed to write report: {}", "⚠".yellow(), e);
        }
    }

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
                println!("{} ({})", "WARNING".yellow().bold(), resp.status());
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

async fn run_all_tests(
    config: &Config,
    concurrency: usize,
    duration: u64,
    endpoints: Option<String>,
    report: Option<Arc<Mutex<MarkdownReport>>>,
) {
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
    if let Some(result) =
        stress::run_stress_test_with_result(config, concurrency, duration, "http-get", "/").await
    {
        if let Some(ref rpt) = report {
            rpt.lock().await.add_result(
                "HTTP GET",
                "http-get",
                concurrency,
                std::time::Duration::from_secs(duration),
                &result,
            );
        }
    }

    println!("\n{}", "  HTTP POST Stress Test".cyan());
    if let Some(result) =
        stress::run_stress_test_with_result(config, concurrency, duration, "http-post", "/").await
    {
        if let Some(ref rpt) = report {
            rpt.lock().await.add_result(
                "HTTP POST",
                "http-post",
                concurrency,
                std::time::Duration::from_secs(duration),
                &result,
            );
        }
    }

    println!("\n{}", "  WebSocket Stress Test".cyan());
    if let Some(result) =
        stress::run_stress_test_with_result(config, concurrency, duration, "websocket", "/").await
    {
        if let Some(ref rpt) = report {
            rpt.lock().await.add_result(
                "WebSocket",
                "websocket",
                concurrency,
                std::time::Duration::from_secs(duration),
                &result,
            );
        }
    }

    println!("\n{}", "  Mixed Stress Test".cyan());
    if let Some(result) =
        stress::run_stress_test_with_result(config, concurrency, duration, "mixed", "/").await
    {
        if let Some(ref rpt) = report {
            rpt.lock().await.add_result(
                "Mixed Workload",
                "mixed",
                concurrency,
                std::time::Duration::from_secs(duration),
                &result,
            );
        }
    }

    println!();
    println!("{}", "═══ All Tests Complete ═══".green().bold());
}
