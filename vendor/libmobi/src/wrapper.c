#include <stdio.h>
#include <string.h>
#include "mobi.h"

#define MINIZ_HEADER_FILE_ONLY
#define MINIZ_NO_ZLIB_COMPATIBLE_NAMES
#include "miniz.c"

static const char container_xml[] =
    "<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
    "<container version=\"1.0\" xmlns=\"urn:oasis:names:tc:opendocument:xmlns:container\">"
    "<rootfiles><rootfile full-path=\"OEBPS/content.opf\" "
    "media-type=\"application/oebps-package+xml\"/></rootfiles></container>";

static int fail(char *error, size_t error_size, const char *message) {
    if (error && error_size) snprintf(error, error_size, "%s", message);
    return 1;
}

static int add_part(mz_zip_archive *zip, const char *prefix, const MOBIPart *part) {
    MOBIFileMeta meta = mobi_get_filemeta_by_type(part->type);
    char name[1024];
    if (meta.type == T_OPF) {
        snprintf(name, sizeof(name), "OEBPS/content.opf");
    } else {
        snprintf(name, sizeof(name), "OEBPS/%s%05zu.%s", prefix, part->uid, meta.extension);
    }
    return mz_zip_writer_add_mem(zip, name, part->data, part->size, MZ_DEFAULT_COMPRESSION) ? 0 : 1;
}

int et_mobi_to_epub(const char *input, const char *output, char *error, size_t error_size) {
    int result = 1;
    FILE *file = NULL;
    MOBIData *mobi = NULL;
    MOBIRawml *rawml = NULL;
    mz_zip_archive zip;
    memset(&zip, 0, sizeof(zip));

    mobi = mobi_init();
    if (!mobi) return fail(error, error_size, "libmobi: memory allocation failed");
    file = fopen(input, "rb");
    if (!file) { fail(error, error_size, "libmobi: cannot open input"); goto cleanup; }
    MOBI_RET ret = mobi_load_file(mobi, file);
    fclose(file); file = NULL;
    if (ret != MOBI_SUCCESS) { fail(error, error_size, "libmobi: cannot load document"); goto cleanup; }
    if (mobi_is_replica(mobi)) { fail(error, error_size, "libmobi: print replica requires calibre"); goto cleanup; }
    rawml = mobi_init_rawml(mobi);
    if (!rawml) { fail(error, error_size, "libmobi: rawml allocation failed"); goto cleanup; }
    ret = mobi_parse_rawml(rawml, mobi);
    if (ret != MOBI_SUCCESS) { fail(error, error_size, "libmobi: cannot parse document"); goto cleanup; }

    if (!mz_zip_writer_init_file(&zip, output, 0)) { fail(error, error_size, "libmobi: cannot create EPUB"); goto cleanup; }
    if (!mz_zip_writer_add_mem(&zip, "mimetype", "application/epub+zip", 20, MZ_NO_COMPRESSION) ||
        !mz_zip_writer_add_mem(&zip, "META-INF/container.xml", container_xml, sizeof(container_xml) - 1, MZ_DEFAULT_COMPRESSION)) {
        fail(error, error_size, "libmobi: cannot initialize EPUB"); goto zip_cleanup;
    }
    for (MOBIPart *part = rawml->markup; part; part = part->next) {
        if (add_part(&zip, "part", part)) { fail(error, error_size, "libmobi: cannot write markup"); goto zip_cleanup; }
    }
    if (rawml->flow) {
        for (MOBIPart *part = rawml->flow->next; part; part = part->next) {
            if (add_part(&zip, "flow", part)) { fail(error, error_size, "libmobi: cannot write flow"); goto zip_cleanup; }
        }
    }
    for (MOBIPart *part = rawml->resources; part; part = part->next) {
        if (part->size && add_part(&zip, "resource", part)) { fail(error, error_size, "libmobi: cannot write resource"); goto zip_cleanup; }
    }
    if (!mz_zip_writer_finalize_archive(&zip)) { fail(error, error_size, "libmobi: cannot finalize EPUB"); goto zip_cleanup; }
    result = 0;

zip_cleanup:
    mz_zip_writer_end(&zip);
cleanup:
    if (file) fclose(file);
    if (rawml) mobi_free_rawml(rawml);
    if (mobi) mobi_free(mobi);
    if (result) remove(output);
    return result;
}
