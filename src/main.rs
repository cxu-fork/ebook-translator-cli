mod cache;
mod config;
mod converter;
mod engine;
mod epub;
mod fsutil;
mod glossary;
mod worker;

mod cli;

#[tokio::main]
async fn main() {
    std::process::exit(cli::run().await);
}
