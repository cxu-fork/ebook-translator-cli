fn main() {
    let root = "vendor/libmobi/src";
    let mut build = cc::Build::new();
    build
        .include(root)
        .define("PACKAGE_VERSION", "\"0.12\"")
        .define("USE_XMLWRITER", None)
        .define("USE_MINIZ", None)
        .define("MOBI_INLINE", "inline")
        .warnings(false);
    if std::env::var("CARGO_CFG_WINDOWS").is_err() {
        build
            .define("HAVE_STRDUP", "1")
            .define("HAVE_UNISTD_H", "1")
            .define("_POSIX_C_SOURCE", "200112L");
    }
    for file in [
        "buffer.c",
        "compression.c",
        "debug.c",
        "index.c",
        "memory.c",
        "meta.c",
        "miniz.c",
        "opf.c",
        "parse_rawml.c",
        "read.c",
        "structure.c",
        "util.c",
        "write.c",
        "xmlwriter.c",
        "wrapper.c",
    ] {
        build.file(format!("{root}/{file}"));
    }
    build.compile("mobi_embedded");
    println!("cargo:rerun-if-changed={root}");
}
