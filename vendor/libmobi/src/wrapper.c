#include <stdio.h>
#include <string.h>
#ifdef _WIN32
#include <wchar.h>
#endif
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

static int convert_open(FILE *file, mz_zip_archive *zip, char *error, size_t error_size) {
    int result = 1;
    MOBIData *mobi = NULL;
    MOBIRawml *rawml = NULL;

    mobi = mobi_init();
    if (!mobi) { fail(error, error_size, "libmobi: memory allocation failed"); goto zip_cleanup; }
    MOBI_RET ret = mobi_load_file(mobi, file);
    fclose(file); file = NULL;
    if (ret != MOBI_SUCCESS) { fail(error, error_size, "libmobi: cannot load document"); goto zip_cleanup; }
    if (mobi_is_replica(mobi)) { fail(error, error_size, "libmobi: print replica requires calibre"); goto zip_cleanup; }
    rawml = mobi_init_rawml(mobi);
    if (!rawml) { fail(error, error_size, "libmobi: rawml allocation failed"); goto zip_cleanup; }
    ret = mobi_parse_rawml(rawml, mobi);
    if (ret != MOBI_SUCCESS) { fail(error, error_size, "libmobi: cannot parse document"); goto zip_cleanup; }

    if (!mz_zip_writer_add_mem(zip, "mimetype", "application/epub+zip", 20, MZ_NO_COMPRESSION) ||
        !mz_zip_writer_add_mem(zip, "META-INF/container.xml", container_xml, sizeof(container_xml) - 1, MZ_DEFAULT_COMPRESSION)) {
        fail(error, error_size, "libmobi: cannot initialize EPUB"); goto zip_cleanup;
    }
    for (MOBIPart *part = rawml->markup; part; part = part->next) {
        if (add_part(zip, "part", part)) { fail(error, error_size, "libmobi: cannot write markup"); goto zip_cleanup; }
    }
    if (rawml->flow) {
        for (MOBIPart *part = rawml->flow->next; part; part = part->next) {
            if (add_part(zip, "flow", part)) { fail(error, error_size, "libmobi: cannot write flow"); goto zip_cleanup; }
        }
    }
    for (MOBIPart *part = rawml->resources; part; part = part->next) {
        if (part->size && add_part(zip, "resource", part)) { fail(error, error_size, "libmobi: cannot write resource"); goto zip_cleanup; }
    }
    if (!mz_zip_writer_finalize_archive(zip)) { fail(error, error_size, "libmobi: cannot finalize EPUB"); goto zip_cleanup; }
    result = 0;

zip_cleanup:
    mz_zip_writer_end(zip);
cleanup:
    if (file) fclose(file);
    if (rawml) mobi_free_rawml(rawml);
    if (mobi) mobi_free(mobi);
    return result;
}

int et_mobi_to_epub(const char *input, const char *output, char *error, size_t error_size) {
    mz_zip_archive zip;
    memset(&zip, 0, sizeof(zip));
    FILE *file = fopen(input, "rb");
    if (!file) return fail(error, error_size, "libmobi: cannot open input");
    if (!mz_zip_writer_init_file(&zip, output, 0)) {
        fclose(file);
        return fail(error, error_size, "libmobi: cannot create EPUB");
    }
    int result = convert_open(file, &zip, error, error_size);
    if (result) remove(output);
    return result;
}

#ifdef _WIN32
static mz_bool mz_zip_writer_init_wfile(mz_zip_archive *zip, const wchar_t *filename) {
    zip->m_pWrite = mz_zip_file_write_func;
    zip->m_pIO_opaque = zip;
    if (!mz_zip_writer_init(zip, 0)) return MZ_FALSE;
    zip->m_pState->m_pFile = _wfopen(filename, L"wb");
    if (!zip->m_pState->m_pFile) {
        mz_zip_writer_end(zip);
        return MZ_FALSE;
    }
    return MZ_TRUE;
}

int et_mobi_to_epub_w(const wchar_t *input, const wchar_t *output, char *error, size_t error_size) {
    mz_zip_archive zip;
    memset(&zip, 0, sizeof(zip));
    FILE *file = _wfopen(input, L"rb");
    if (!file) return fail(error, error_size, "libmobi: cannot open input");
    if (!mz_zip_writer_init_wfile(&zip, output)) {
        fclose(file);
        return fail(error, error_size, "libmobi: cannot create EPUB");
    }
    int result = convert_open(file, &zip, error, error_size);
    if (result) _wremove(output);
    return result;
}
#endif
