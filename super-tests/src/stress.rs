//! Stress testing module for measuring RPS and server performance.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use colored::Colorize;
use futures_util::{SinkExt, StreamExt};
use indicatif::{ProgressBar, ProgressStyle};
use reqwest::Client;
use serde_json::json;
use tokio::time::timeout;
use tokio_tungstenite::{connect_async, tungstenite::Message};

use crate::config::Config;
use crate::stats::{Stats, StressTestResult, Timer};

/// Run a stress test based on the specified type.
pub async fn run_stress_test(
    config: &Config,
    concurrency: usize,
    duration_secs: u64,
    test_type: &str,
    endpoint: &str,
) {
    println!(
        "    {} {} concurrent workers for {}s\n",
        "Running".blue(),
        concurrency.to_string().cyan(),
        duration_secs
    );

    let stats = Stats::new();
    let running = Arc::new(AtomicBool::new(true));
    let start_time = Instant::now();

    // Create progress bar
    let pb = ProgressBar::new(duration_secs);
    pb.set_style(
        ProgressStyle::default_bar()
            .template("    [{elapsed_precise}] {bar:40.cyan/blue} {pos:>2}/{len:2}s | {msg}")
            .unwrap()
            .progress_chars("█▓░"),
    );

    // Spawn the timer task
    let pb_clone = pb.clone();
    let running_clone = running.clone();
    let stats_clone = stats.clone();
    let timer_handle = tokio::spawn(async move {
        let mut elapsed = 0;
        while elapsed < duration_secs && running_clone.load(Ordering::Relaxed) {
            tokio::time::sleep(Duration::from_secs(1)).await;
            elapsed += 1;
            pb_clone.set_position(elapsed);

            let current_rps =
                stats_clone.success_count() as f64 / (elapsed as f64).max(1.0);
            pb_clone.set_message(format!(
                "RPS: {:.0} | OK: {} | ERR: {}",
                current_rps,
                stats_clone.success_count(),
                stats_clone.failure_count()
            ));
        }
    });

    // Spawn worker tasks based on test type
    let handles: Vec<_> = match test_type {
        "http-get" => spawn_http_get_workers(config, concurrency, &stats, &running, endpoint),
        "http-post" => spawn_http_post_workers(config, concurrency, &stats, &running, endpoint),
        "websocket" => spawn_websocket_workers(config, concurrency, &stats, &running),
        "mixed" => spawn_mixed_workers(config, concurrency, &stats, &running, endpoint),
        _ => {
            println!("    {} Unknown test type: {}", "Error:".red(), test_type);
            return;
        }
    };

    // Wait for duration
    tokio::time::sleep(Duration::from_secs(duration_secs)).await;
    running.store(false, Ordering::Relaxed);

    // Wait for workers to finish
    for handle in handles {
        let _ = handle.await;
    }

    timer_handle.abort();
    pb.finish_and_clear();

    // Calculate and print results
    let duration = start_time.elapsed();
    let result = StressTestResult::from_stats(&stats, duration);
    result.print_report();
}

/// Spawn HTTP GET stress test workers.
fn spawn_http_get_workers(
    config: &Config,
    concurrency: usize,
    stats: &Arc<Stats>,
    running: &Arc<AtomicBool>,
    endpoint: &str,
) -> Vec<tokio::task::JoinHandle<()>> {
    let client = Client::new();
    let url = format!("{}{}", config.http_url(), endpoint);

    (0..concurrency)
        .map(|_| {
            let client = client.clone();
            let url = url.clone();
            let stats = stats.clone();
            let running = running.clone();

            tokio::spawn(async move {
                while running.load(Ordering::Relaxed) {
                    let timer = Timer::start();
                    match client.get(&url).send().await {
                        Ok(resp) => {
                            let bytes = resp.bytes().await.map(|b| b.len()).unwrap_or(0);
                            stats.record_success(timer.elapsed(), bytes);
                        }
                        Err(_) => {
                            stats.record_failure();
                        }
                    }
                }
            })
        })
        .collect()
}

/// Spawn HTTP POST stress test workers.
fn spawn_http_post_workers(
    config: &Config,
    concurrency: usize,
    stats: &Arc<Stats>,
    running: &Arc<AtomicBool>,
    endpoint: &str,
) -> Vec<tokio::task::JoinHandle<()>> {
    let client = Client::new();
    let url = format!("{}{}", config.http_url(), endpoint);

    (0..concurrency)
        .map(|i| {
            let client = client.clone();
            let url = url.clone();
            let stats = stats.clone();
            let running = running.clone();

            tokio::spawn(async move {
                let mut counter = 0u64;
                while running.load(Ordering::Relaxed) {
                    let body = json!({
                        "worker": i,
                        "request": counter,
                        "timestamp": chrono::Utc::now().timestamp_millis()
                    });
                    counter += 1;

                    let timer = Timer::start();
                    match client
                        .post(&url)
                        .header("Content-Type", "application/json")
                        .json(&body)
                        .send()
                        .await
                    {
                        Ok(resp) => {
                            let bytes = resp.bytes().await.map(|b| b.len()).unwrap_or(0);
                            stats.record_success(timer.elapsed(), bytes);
                        }
                        Err(_) => {
                            stats.record_failure();
                        }
                    }
                }
            })
        })
        .collect()
}

/// Spawn WebSocket stress test workers.
fn spawn_websocket_workers(
    config: &Config,
    concurrency: usize,
    stats: &Arc<Stats>,
    running: &Arc<AtomicBool>,
) -> Vec<tokio::task::JoinHandle<()>> {
    let ws_url = config.ws_url();

    (0..concurrency)
        .map(|i| {
            let ws_url = ws_url.clone();
            let stats = stats.clone();
            let running = running.clone();

            tokio::spawn(async move {
                while running.load(Ordering::Relaxed) {
                    // Try to establish connection
                    match timeout(Duration::from_secs(5), connect_async(&ws_url)).await {
                        Ok(Ok((mut ws, _))) => {
                            let mut msg_counter = 0u64;

                            // Send and receive messages while running
                            while running.load(Ordering::Relaxed) {
                                let msg = json!({
                                    "worker": i,
                                    "msg": msg_counter
                                });
                                msg_counter += 1;

                                let timer = Timer::start();

                                // Send message
                                if ws.send(Message::Text(msg.to_string())).await.is_err() {
                                    stats.record_failure();
                                    break;
                                }

                                // Wait for response
                                match timeout(Duration::from_secs(5), ws.next()).await {
                                    Ok(Some(Ok(Message::Text(text)))) => {
                                        stats.record_success(timer.elapsed(), text.len());
                                    }
                                    _ => {
                                        stats.record_failure();
                                        break;
                                    }
                                }
                            }

                            let _ = ws.close(None).await;
                        }
                        _ => {
                            stats.record_failure();
                            // Wait a bit before retrying connection
                            tokio::time::sleep(Duration::from_millis(100)).await;
                        }
                    }
                }
            })
        })
        .collect()
}

/// Spawn mixed workload stress test workers.
fn spawn_mixed_workers(
    config: &Config,
    concurrency: usize,
    stats: &Arc<Stats>,
    running: &Arc<AtomicBool>,
    endpoint: &str,
) -> Vec<tokio::task::JoinHandle<()>> {
    let client = Client::new();
    let http_url = format!("{}{}", config.http_url(), endpoint);
    let ws_url = config.ws_url();

    (0..concurrency)
        .map(|i| {
            let client = client.clone();
            let http_url = http_url.clone();
            let ws_url = ws_url.clone();
            let stats = stats.clone();
            let running = running.clone();

            tokio::spawn(async move {
                let mut counter = 0u64;

                while running.load(Ordering::Relaxed) {
                    // Rotate through different request types
                    match counter % 4 {
                        0 => {
                            // HTTP GET
                            let timer = Timer::start();
                            match client.get(&http_url).send().await {
                                Ok(resp) => {
                                    let bytes = resp.bytes().await.map(|b| b.len()).unwrap_or(0);
                                    stats.record_success(timer.elapsed(), bytes);
                                }
                                Err(_) => stats.record_failure(),
                            }
                        }
                        1 => {
                            // HTTP POST
                            let body = json!({"mixed": counter, "worker": i});
                            let timer = Timer::start();
                            match client
                                .post(&http_url)
                                .header("Content-Type", "application/json")
                                .json(&body)
                                .send()
                                .await
                            {
                                Ok(resp) => {
                                    let bytes = resp.bytes().await.map(|b| b.len()).unwrap_or(0);
                                    stats.record_success(timer.elapsed(), bytes);
                                }
                                Err(_) => stats.record_failure(),
                            }
                        }
                        2 | 3 => {
                            // WebSocket (quick send/receive cycle)
                            match timeout(Duration::from_secs(2), connect_async(&ws_url)).await {
                                Ok(Ok((mut ws, _))) => {
                                    let msg = json!({"ws_mixed": counter});
                                    let timer = Timer::start();

                                    if ws.send(Message::Text(msg.to_string())).await.is_ok() {
                                        match timeout(Duration::from_secs(2), ws.next()).await {
                                            Ok(Some(Ok(Message::Text(text)))) => {
                                                stats.record_success(timer.elapsed(), text.len());
                                            }
                                            _ => stats.record_failure(),
                                        }
                                    } else {
                                        stats.record_failure();
                                    }

                                    let _ = ws.close(None).await;
                                }
                                _ => stats.record_failure(),
                            }
                        }
                        _ => {}
                    }

                    counter += 1;
                }
            })
        })
        .collect()
}

/// Specialized benchmark for measuring peak RPS.
pub async fn run_peak_rps_test(config: &Config, endpoint: &str) {
    println!(
        "    {} Peak RPS measurement...\n",
        "Running".blue()
    );

    let concurrency_levels = [1, 10, 50, 100, 200, 500];
    let test_duration = Duration::from_secs(5);

    println!(
        "    {:<12} {:<12} {:<12} {:<12}",
        "Workers".white().bold(),
        "RPS".white().bold(),
        "Avg Lat".white().bold(),
        "Errors".white().bold()
    );
    println!("    {}", "─".repeat(48));

    let client = Client::new();
    let url = format!("{}{}", config.http_url(), endpoint);

    for &concurrency in &concurrency_levels {
        let stats = Stats::new();
        let running = Arc::new(AtomicBool::new(true));
        let start = Instant::now();

        // Spawn workers
        let handles: Vec<_> = (0..concurrency)
            .map(|_| {
                let client = client.clone();
                let url = url.clone();
                let stats = stats.clone();
                let running = running.clone();

                tokio::spawn(async move {
                    while running.load(Ordering::Relaxed) {
                        let timer = Timer::start();
                        match client.get(&url).send().await {
                            Ok(resp) => {
                                let bytes = resp.bytes().await.map(|b| b.len()).unwrap_or(0);
                                stats.record_success(timer.elapsed(), bytes);
                            }
                            Err(_) => stats.record_failure(),
                        }
                    }
                })
            })
            .collect();

        // Run for test duration
        tokio::time::sleep(test_duration).await;
        running.store(false, Ordering::Relaxed);

        // Wait for workers
        for handle in handles {
            let _ = handle.await;
        }

        let duration = start.elapsed();
        let result = StressTestResult::from_stats(&stats, duration);

        let error_str = if result.failed_requests > 0 {
            format!("{}", result.failed_requests).red().to_string()
        } else {
            "0".green().to_string()
        };

        println!(
            "    {:<12} {:<12} {:<12} {:<12}",
            concurrency,
            format!("{:.0}", result.rps).yellow(),
            format!("{:.2}ms", result.avg_latency.as_secs_f64() * 1000.0),
            error_str
        );
    }

    println!();
}
