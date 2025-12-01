//! Statistics module for tracking test metrics.

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use colored::Colorize;

/// Thread-safe statistics collector for stress tests.
#[derive(Debug, Default)]
pub struct Stats {
    /// Total number of successful requests
    pub success: AtomicU64,
    /// Total number of failed requests
    pub failures: AtomicU64,
    /// Total bytes received
    pub bytes_received: AtomicU64,
    /// Minimum latency in microseconds
    pub min_latency_us: AtomicU64,
    /// Maximum latency in microseconds
    pub max_latency_us: AtomicU64,
    /// Sum of all latencies for average calculation
    pub total_latency_us: AtomicU64,
}

impl Stats {
    /// Create a new Stats instance.
    pub fn new() -> Arc<Self> {
        Arc::new(Self {
            success: AtomicU64::new(0),
            failures: AtomicU64::new(0),
            bytes_received: AtomicU64::new(0),
            min_latency_us: AtomicU64::new(u64::MAX),
            max_latency_us: AtomicU64::new(0),
            total_latency_us: AtomicU64::new(0),
        })
    }

    /// Record a successful request.
    pub fn record_success(&self, latency: Duration, bytes: usize) {
        self.success.fetch_add(1, Ordering::Relaxed);
        self.bytes_received.fetch_add(bytes as u64, Ordering::Relaxed);

        let latency_us = latency.as_micros() as u64;
        self.total_latency_us.fetch_add(latency_us, Ordering::Relaxed);

        // Update min latency (atomic min)
        let mut current = self.min_latency_us.load(Ordering::Relaxed);
        while latency_us < current {
            match self.min_latency_us.compare_exchange_weak(
                current,
                latency_us,
                Ordering::Relaxed,
                Ordering::Relaxed,
            ) {
                Ok(_) => break,
                Err(x) => current = x,
            }
        }

        // Update max latency (atomic max)
        let mut current = self.max_latency_us.load(Ordering::Relaxed);
        while latency_us > current {
            match self.max_latency_us.compare_exchange_weak(
                current,
                latency_us,
                Ordering::Relaxed,
                Ordering::Relaxed,
            ) {
                Ok(_) => break,
                Err(x) => current = x,
            }
        }
    }

    /// Record a failed request.
    pub fn record_failure(&self) {
        self.failures.fetch_add(1, Ordering::Relaxed);
    }

    /// Get total requests count.
    pub fn total(&self) -> u64 {
        self.success.load(Ordering::Relaxed) + self.failures.load(Ordering::Relaxed)
    }

    /// Get success count.
    pub fn success_count(&self) -> u64 {
        self.success.load(Ordering::Relaxed)
    }

    /// Get failure count.
    pub fn failure_count(&self) -> u64 {
        self.failures.load(Ordering::Relaxed)
    }
}

/// Result of a stress test run.
#[derive(Debug)]
pub struct StressTestResult {
    /// Total duration of the test
    pub duration: Duration,
    /// Total number of requests made
    pub total_requests: u64,
    /// Number of successful requests
    pub successful_requests: u64,
    /// Number of failed requests
    pub failed_requests: u64,
    /// Requests per second
    pub rps: f64,
    /// Average latency
    pub avg_latency: Duration,
    /// Minimum latency
    pub min_latency: Duration,
    /// Maximum latency
    pub max_latency: Duration,
    /// Total bytes transferred
    pub bytes_transferred: u64,
    /// Throughput in MB/s
    pub throughput_mbps: f64,
}

impl StressTestResult {
    /// Create a result from stats and test duration.
    pub fn from_stats(stats: &Stats, duration: Duration) -> Self {
        let total = stats.total();
        let success = stats.success_count();
        let failures = stats.failure_count();
        let total_latency = stats.total_latency_us.load(Ordering::Relaxed);
        let bytes = stats.bytes_received.load(Ordering::Relaxed);

        let rps = if duration.as_secs_f64() > 0.0 {
            total as f64 / duration.as_secs_f64()
        } else {
            0.0
        };

        let avg_latency = if success > 0 {
            Duration::from_micros(total_latency / success)
        } else {
            Duration::ZERO
        };

        let min_latency_us = stats.min_latency_us.load(Ordering::Relaxed);
        let min_latency = if min_latency_us == u64::MAX {
            Duration::ZERO
        } else {
            Duration::from_micros(min_latency_us)
        };

        let max_latency = Duration::from_micros(stats.max_latency_us.load(Ordering::Relaxed));

        let throughput_mbps = if duration.as_secs_f64() > 0.0 {
            (bytes as f64 / 1024.0 / 1024.0) / duration.as_secs_f64()
        } else {
            0.0
        };

        Self {
            duration,
            total_requests: total,
            successful_requests: success,
            failed_requests: failures,
            rps,
            avg_latency,
            min_latency,
            max_latency,
            bytes_transferred: bytes,
            throughput_mbps,
        }
    }

    /// Print a formatted report.
    pub fn print_report(&self) {
        println!();
        println!("  {}", "─── Results ───".white().bold());
        println!(
            "    {} {:>12}",
            "Duration:".white(),
            format!("{:.2}s", self.duration.as_secs_f64())
        );
        println!(
            "    {} {:>12}",
            "Total Requests:".white(),
            self.total_requests
        );
        println!(
            "    {} {:>12} {}",
            "Successful:".white(),
            self.successful_requests,
            format!(
                "({:.1}%)",
                if self.total_requests > 0 {
                    (self.successful_requests as f64 / self.total_requests as f64) * 100.0
                } else {
                    0.0
                }
            )
            .green()
        );
        println!(
            "    {} {:>12} {}",
            "Failed:".white(),
            self.failed_requests,
            if self.failed_requests > 0 {
                format!(
                    "({:.1}%)",
                    (self.failed_requests as f64 / self.total_requests as f64) * 100.0
                )
                .red()
                .to_string()
            } else {
                "(0%)".green().to_string()
            }
        );
        println!();
        println!(
            "    {} {:>12}",
            "RPS:".cyan().bold(),
            format!("{:.2}", self.rps).yellow().bold()
        );
        println!();
        println!("  {}", "─── Latency ───".white().bold());
        println!(
            "    {} {:>12}",
            "Min:".white(),
            format_duration(self.min_latency)
        );
        println!(
            "    {} {:>12}",
            "Avg:".white(),
            format_duration(self.avg_latency)
        );
        println!(
            "    {} {:>12}",
            "Max:".white(),
            format_duration(self.max_latency)
        );
        println!();
        println!("  {}", "─── Throughput ───".white().bold());
        println!(
            "    {} {:>12}",
            "Bytes:".white(),
            format_bytes(self.bytes_transferred)
        );
        println!(
            "    {} {:>12}",
            "Rate:".white(),
            format!("{:.2} MB/s", self.throughput_mbps)
        );
    }
}

/// Format a duration for display.
fn format_duration(d: Duration) -> String {
    let us = d.as_micros();
    if us < 1000 {
        format!("{}µs", us)
    } else if us < 1_000_000 {
        format!("{:.2}ms", us as f64 / 1000.0)
    } else {
        format!("{:.2}s", us as f64 / 1_000_000.0)
    }
}

/// Format bytes for display.
fn format_bytes(bytes: u64) -> String {
    if bytes < 1024 {
        format!("{} B", bytes)
    } else if bytes < 1024 * 1024 {
        format!("{:.2} KB", bytes as f64 / 1024.0)
    } else if bytes < 1024 * 1024 * 1024 {
        format!("{:.2} MB", bytes as f64 / 1024.0 / 1024.0)
    } else {
        format!("{:.2} GB", bytes as f64 / 1024.0 / 1024.0 / 1024.0)
    }
}

/// Timer for measuring request latency.
pub struct Timer {
    start: Instant,
}

impl Timer {
    /// Start a new timer.
    pub fn start() -> Self {
        Self {
            start: Instant::now(),
        }
    }

    /// Get elapsed duration.
    pub fn elapsed(&self) -> Duration {
        self.start.elapsed()
    }
}
