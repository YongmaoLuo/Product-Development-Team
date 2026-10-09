/*
 * pdt-secret-reader — read one generic password out of one keychain.
 *
 * Why this exists
 * ---------------
 * The backend reads its notification secrets by shelling out to
 * /usr/bin/security. That works, and it is also why the keychain item's
 * access control list trusts /usr/bin/security — an Apple-signed binary
 * that *every* process on the machine may execute. So the ACL grants the
 * secret to anything that can run one command, and the read is silent
 * because the entry it matches is already there. Nothing about that is a
 * bug in this project; it is what shelling out to a shared tool buys.
 *
 * This program is the same read with a narrower ACL: it is signed with a
 * local identity, and the item is re-filed so the only trusted
 * application is *this binary*. The old path keeps working — it just
 * stops being trusted, which means it starts asking instead of answering.
 *
 * What it is not
 * --------------
 * Not a security boundary, and it should not be sold as one. A process
 * running as this user can still exec this binary, and can still be
 * social-engineered through a permission dialog. It removes the *silent*
 * path — a read that leaves no trace and needs no consent — and that is
 * all it claims.
 *
 * argv compatibility
 * ------------------
 * It accepts the exact command line ``credentials._resolve_from_keychain``
 * already builds:
 *
 *     pdt-secret-reader find-generic-password -a <account> -w <keychain>
 *
 * so switching the backend over is pointing one constant at this file
 * instead of at /usr/bin/security, and switching back is pointing it
 * back. There is also a short form:
 *
 *     pdt-secret-reader <account> <keychain>
 *
 * Both forms name the keychain explicitly, and neither has a default:
 * see require_keychain() below. A keychain is a property of the
 * installation, not of this program, so every caller passes one — there
 * is no invocation of either form that leaves it out.
 *
 * Output
 * ------
 * The password, raw, on stdout, with no trailing newline — a secret is
 * bytes, and a terminator would be a byte the reader has to strip. The
 * value is never written to argv, to a file, or to stderr.
 *
 * Exit codes
 * ----------
 * 0 success, 2 usage, 3 stdout is a terminal, 4 keychain would not open,
 * 5 no such item, 6 the item's data could not be read, 7 the item refused
 * this process, 8 it would have to ask and there is nobody to ask. Distinct
 * codes so a caller can tell "the operator has not filed this" from "this
 * process is not allowed to look".
 */

#include <CoreFoundation/CoreFoundation.h>
#include <Security/Security.h>

#include <errno.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

enum {
    exit_ok = 0,
    exit_usage = 2,
    exit_tty = 3,
    exit_no_keychain = 4,
    exit_no_item = 5,
    exit_no_data = 6,
    exit_not_authorized = 7,
    exit_no_interaction = 8,
};

static void fail(int code, const char *what, OSStatus status)
{
    if (status == 0) {
        fprintf(stderr, "pdt-secret-reader: %s\n", what);
    } else {
        char detail[512] = {0};
        CFStringRef message = SecCopyErrorMessageString(status, NULL);
        if (message != NULL) {
            CFStringGetCString(message, detail, sizeof detail,
                               kCFStringEncodingUTF8);
            CFRelease(message);
        }
        fprintf(stderr, "pdt-secret-reader: %s: %s (%d)\n", what,
                detail[0] ? detail : "no description", (int)status);
    }
    exit(code);
}

static void usage(void)
{
    fprintf(stderr,
            "usage: pdt-secret-reader find-generic-password -a <account>"
            " [-w] <keychain>\n"
            "       pdt-secret-reader <account> <keychain>\n"
            "       pdt-secret-reader --adopt -a <account> [-s <service>]\n"
            "                          [--also <appPath>]... <keychain>\n");
    exit(exit_usage);
}

/* A keychain is always named explicitly.
 *
 * There is deliberately no default. Which keychain an installation files
 * its items in is a property of that installation; this program is meant
 * to be usable on machines whose layout is not the one it was written on,
 * and a path compiled in here would be a guess about somebody else's
 * machine written into their source. Every caller that needs one passes
 * one. */
static const char *require_keychain(const char *given)
{
    if (given == NULL || given[0] == '\0') {
        fail(exit_usage, "no keychain given", 0);
    }
    return given;
}

/* Which exit code an OSStatus from touching an item maps to.
 *
 * Two of them get a code of their own because neither is "no such item",
 * and an operator reading "no item for that account" goes looking for a
 * credential that is on disk and readable by hand:
 *
 *   errSecAuthFailed            the item is there and refused this binary.
 *                               Nothing about filing it differently helps;
 *                               the access list is the thing to look at.
 *   errSecInteractionNotAllowed it would have had to ask, and there is
 *                               nobody here to answer — a launchd job, a
 *                               daemon, anything with no session to put a
 *                               dialog in front of. Retrying does nothing.
 */
static int status_exit_code(OSStatus status)
{
    switch (status) {
    case errSecAuthFailed:            return exit_not_authorized;
    case errSecInteractionNotAllowed: return exit_no_interaction;
    default:                          return exit_no_item;
    }
}

/* Open a keychain the caller has already resolved to a path.
 *
 * The `stat` is not belt-and-braces. `SecKeychainOpen` succeeds on a path
 * that does not exist — it hands back a reference and lets the first use
 * fail — so a typo'd path would surface as "no item for that account",
 * sending the operator to look for a credential that was never filed
 * rather than at the path they just mistyped. Measured, not assumed: a
 * bogus path returned errSecItemNotFound (-25300) before this check
 * existed. */
static SecKeychainRef open_keychain(const char *path)
{
    struct stat st;
    SecKeychainRef keychain = NULL;
    OSStatus status;

    if (stat(path, &st) != 0) {
        fprintf(stderr, "pdt-secret-reader: no keychain at %s: %s\n",
                path, strerror(errno));
        exit(exit_no_keychain);
    }

    status = SecKeychainOpen(path, &keychain);
    if (status != errSecSuccess) {
        fail(exit_no_keychain, "cannot open the keychain", status);
    }
    return keychain;
}

/* ------------------------------------------------------------------ */
/* --adopt                                                            */
/* ------------------------------------------------------------------ */

/*
 * Rewrite one item's own access control list.
 *
 * This is the password-free way to narrow an item. The alternative,
 * `security add-generic-password -T`, rebuilds the item from its value —
 * which means reading the secret out and writing it back. This rewrites
 * the access object the item already has and never touches the value at
 * all.
 *
 * What "the item's own" is worth, and what the version this replaced got
 * wrong: that one built a *new* access object with SecAccessCreate and
 * handed it to SecKeychainItemSetAccess, and the comment here claimed the
 * rewrite was in place. Measured in a throwaway keychain, it was not. The
 * item's original ACLs came out the other side untouched — still naming
 * /bin/ls and /bin/cat — with the new list added beside them as a fifth
 * ACL. On the real machine that is why `/usr/bin/security` kept reading the
 * items silently after a --narrow: it had never come off the list, only a
 * second list had been added. So the access object now comes from
 * SecKeychainItemCopyAccess, and every application list it holds is
 * replaced rather than added to. That call is the whole difference.
 *
 * Each such list is rewritten to exactly [this binary] plus whatever
 * --also named, without consulting what was on it before. That is what
 * makes --adopt idempotent: a second run computes the same list from the
 * same inputs and writes the same list back, with no state carried between
 * runs. Keeping an application's entry "unless it is me already" is the
 * version that goes wrong the moment that test is wrong.
 *
 * An ACL whose application list is empty is left alone. Measured: an
 * item's access object carries several ACLs, and only one of them names any
 * application at all. Skipping the empty ones is not a shortcut — an empty
 * list is not a list to narrow, and writing one over it would be inventing
 * an ACL the item never had.
 *
 * The edits are made in memory and committed once, at the end.
 * SecACLSetContents changes the access object this function is holding;
 * SecKeychainItemSetAccess is what puts it back on the item. Any failure
 * before that last call exits without it, so a run that fails on the fifth
 * of five ACLs leaves the item exactly as it was found — which is also why
 * the loop below may fail in the middle rather than roll back.
 *
 * The item is *searched* for, not read. Access control is consulted when
 * an item's content is read, and one thing this program must never do is
 * read it.
 *
 * The trusted application is created from a NULL path, meaning "the
 * binary running right now". That is not a convenience: it makes the
 * requirement stored here the same requirement the system will later
 * evaluate this process against. A requirement written by hand would be
 * a guess at how macOS derives one, and a guess that is subtly wrong
 * produces an item nothing can read.
 *
 * The invariant that keeps this safe: the list always contains this
 * binary. Should everything else go wrong, the one program still trusted
 * is the one that can rewrite the list. An item with no application on it
 * at all breaks that invariant from the other side — nothing can rewrite
 * such a list, this binary included — so that case stops rather than
 * reporting a narrowing that did not happen.
 */
static int adopt_item(SecKeychainRef keychain, const char *account,
                      const char *service, char **also, int also_count)
{
    SecKeychainAttribute attrs[2];
    SecKeychainAttributeList attr_list;
    SecKeychainSearchRef search = NULL;
    SecKeychainItemRef item = NULL;
    SecTrustedApplicationRef application = NULL;
    SecAccessRef access = NULL;
    CFArrayRef acls = NULL;
    CFMutableArrayRef trusted = NULL;
    OSStatus status;
    UInt32 count = 0;
    CFIndex acl_count;
    CFIndex i;
    int rewritten = 0;
    int j;

    attrs[count].tag = kSecAccountItemAttr;
    attrs[count].length = (UInt32)strlen(account);
    attrs[count].data = (void *)account;
    count++;

    if (service != NULL) {
        attrs[count].tag = kSecServiceItemAttr;
        attrs[count].length = (UInt32)strlen(service);
        attrs[count].data = (void *)service;
        count++;
    }

    attr_list.count = count;
    attr_list.attr = attrs;

    status = SecKeychainSearchCreateFromAttributes(
        keychain, kSecGenericPasswordItemClass, &attr_list, &search);
    if (status != errSecSuccess) {
        fail(status_exit_code(status), "cannot search this keychain", status);
    }

    status = SecKeychainSearchCopyNext(search, &item);
    CFRelease(search);
    if (status != errSecSuccess) {
        fail(status_exit_code(status), "no item for that account", status);
    }

    /* The item's own access object, not a fresh one. This call is the
     * whole difference between rewriting a list and adding a second one. */
    status = SecKeychainItemCopyAccess(item, &access);
    if (status != errSecSuccess) {
        fail(exit_no_data, "cannot read the item's access object", status);
    }

    status = SecAccessCopyACLList(access, &acls);
    if (status != errSecSuccess) {
        fail(exit_no_data, "cannot read the item's access control list",
             status);
    }

    /* Built once and handed to every ACL that gets rewritten below.
     * SecACLSetContents retains the array it is given, so one list serves
     * all of them — and every one of them ends up holding the same one. */
    trusted = CFArrayCreateMutable(NULL, (CFIndex)(1 + also_count),
                                   &kCFTypeArrayCallBacks);
    if (trusted == NULL) {
        fail(exit_no_data, "cannot build the trusted-application list", 0);
    }

    status = SecTrustedApplicationCreateFromPath(NULL, &application);
    if (status != errSecSuccess) {
        fail(exit_no_data, "cannot describe this binary", status);
    }
    CFArrayAppendValue(trusted, application);
    CFRelease(application);

    for (j = 0; j < also_count; j++) {
        status = SecTrustedApplicationCreateFromPath(also[j], &application);
        if (status != errSecSuccess) {
            fprintf(stderr,
                    "pdt-secret-reader: cannot describe %s, leaving it out "
                    "(%d)\n", also[j], (int)status);
            continue;
        }
        CFArrayAppendValue(trusted, application);
        CFRelease(application);
    }

    acl_count = CFArrayGetCount(acls);
    for (i = 0; i < acl_count; i++) {
        SecACLRef acl = (SecACLRef)CFArrayGetValueAtIndex(acls, i);
        CFArrayRef apps = NULL;
        CFStringRef description = NULL;
        SecKeychainPromptSelector prompt = 0;

        status = SecACLCopyContents(acl, &apps, &description, &prompt);
        if (status != errSecSuccess) {
            fail(exit_no_data,
                 "cannot read one of the item's access control entries",
                 status);
        }

        /* An empty list is not a list to narrow — see the note above. */
        if (apps == NULL || CFArrayGetCount(apps) == 0) {
            if (apps != NULL) {
                CFRelease(apps);
            }
            if (description != NULL) {
                CFRelease(description);
            }
            continue;
        }
        CFRelease(apps);

        /* The description and the prompt selector are passed back
         * unchanged: this narrows who an item answers to, not what the
         * entry is called or when it is asked for. */
        status = SecACLSetContents(acl, trusted, description, prompt);
        if (status != errSecSuccess) {
            fail(exit_no_data,
                 "cannot rewrite one of the item's access control entries",
                 status);
        }
        CFRelease(description);
        rewritten++;
    }
    CFRelease(trusted);

    /* Nothing has reached the keychain at this point — every edit so far
     * was to the copy of the access object held above. An item whose
     * access object names no application at all has no list to narrow and
     * no way for this binary to be on one, so it stops here rather than
     * writing back an unchanged object and claiming a narrowing. */
    if (rewritten == 0) {
        fail(exit_no_data,
             "nothing on this item's access object names an application, so "
             "there was no list to narrow and nothing was written. This "
             "binary is not on the item either, so it could not have "
             "rewritten one later; add it by hand instead — Keychain "
             "Access, select the item, 'Access Control', '+', and this "
             "binary.",
             0);
    }

    /* The one commit. */
    status = SecKeychainItemSetAccess(item, access);
    CFRelease(acls);
    CFRelease(access);
    CFRelease(item);
    if (status != errSecSuccess) {
        fail(exit_no_data, "cannot write the access control list", status);
    }

    printf("adopted %s: %ld of the item's application lists now name this "
           "binary", account, (long)rewritten);
    for (j = 0; j < also_count; j++) {
        printf(" and %s", also[j]);
    }
    printf("\n");
    return exit_ok;
}

static int run_adopt(int argc, char **argv)
{
    const char *account = NULL;
    const char *service = NULL;
    const char *keychain_path = NULL;
    char *also[8];
    int also_count = 0;
    SecKeychainRef keychain = NULL;
    int i;

    for (i = 2; i < argc; i++) {
        if (strcmp(argv[i], "-a") == 0 && i + 1 < argc) {
            account = argv[++i];
        } else if (strcmp(argv[i], "-s") == 0 && i + 1 < argc) {
            service = argv[++i];
        } else if (strcmp(argv[i], "--also") == 0 && i + 1 < argc) {
            if (also_count >= (int)(sizeof also / sizeof also[0])) {
                fail(exit_usage, "--adopt takes at most 8 --also paths", 0);
            }
            also[also_count++] = argv[++i];
        } else if (argv[i][0] != '-') {
            /* One keychain, named once. Taking the last of several would
             * silently rewrite an invocation the caller believes was
             * answered, against a keychain they did not mean to touch. */
            if (keychain_path != NULL) {
                fprintf(stderr,
                        "pdt-secret-reader: --adopt takes one keychain, and "
                        "'%s' is a second\n", argv[i]);
                usage();
            }
            keychain_path = argv[i];
        } else {
            fail(exit_usage, "unknown option to --adopt", 0);
        }
    }

    if (account == NULL || account[0] == '\0') {
        fail(exit_usage, "no account given (-a)", 0);
    }

    keychain_path = require_keychain(keychain_path);
    keychain = open_keychain(keychain_path);

    i = adopt_item(keychain, account, service, also, also_count);
    CFRelease(keychain);
    return i;
}

int main(int argc, char **argv)
{
    const char *account = NULL;
    const char *keychain_path = NULL;
    const char *service = NULL;
    OSStatus status;

    if (argc >= 2 && strcmp(argv[1], "--adopt") == 0) {
        return run_adopt(argc, argv);
    }

    if (argc >= 2 && strcmp(argv[1], "find-generic-password") == 0) {
        /* The shape the backend already builds. Flags are consumed the
         * way `security` consumes them; anything unrecognised that is
         * not a flag is taken as the keychain, which is how the
         * production command line ends. */
        int i;
        for (i = 2; i < argc; i++) {
            if (strcmp(argv[i], "-a") == 0 && i + 1 < argc) {
                account = argv[++i];
            } else if (strcmp(argv[i], "-s") == 0 && i + 1 < argc) {
                service = argv[++i];
            } else if (strcmp(argv[i], "-w") == 0) {
                /* accepted and ignored: this program only reads the
                 * password, so the flag cannot mean anything else */
            } else if (argv[i][0] == '\0') {
                /* Not a keychain, and it is not a flag either — an empty
                 * argv element passes `argv[i][0] != '-'`, so it used to
                 * be taken as the keychain and then quietly replaced by
                 * the next one. `require_keychain` catches a lone empty
                 * string; this catches one sitting anywhere in the list. */
                fprintf(stderr, "pdt-secret-reader: empty argument where the "
                                "keychain goes\n");
                usage();
            } else if (argv[i][0] != '-') {
                /* One keychain, named once — the rule run_adopt already
                 * follows, and the worse of the two. Taking the last of
                 * several does not rewrite anything here: it reads a
                 * *different secret* than the one that was asked for and
                 * hands it back as the answer. */
                if (keychain_path != NULL) {
                    fprintf(stderr, "pdt-secret-reader: this form takes one "
                                    "keychain, and '%s' is a second\n",
                            argv[i]);
                    usage();
                }
                keychain_path = argv[i];
            } else {
                /* Not `security`: an option this build does not know is a
                 * caller building a command line for something else, and
                 * dropping it turns their mistake into a silent read from
                 * the wrong place rather than a stopped run. */
                fprintf(stderr, "pdt-secret-reader: unknown option '%s'\n",
                        argv[i]);
                usage();
            }
        }
    } else if (argc >= 2) {
        /* The short form has no option parser, so a third argument would be
         * ignored outright rather than misread — the same silence as the
         * two-keychain case above, one position later. */
        account = argv[1];
        if (argc >= 3) {
            keychain_path = argv[2];
            if (argc > 3) {
                fprintf(stderr, "pdt-secret-reader: <account> <keychain> "
                                "takes two arguments, and '%s' is a third\n",
                        argv[3]);
                usage();
            }
        }
    } else {
        usage();
    }

    if (account == NULL || account[0] == '\0') {
        fail(exit_usage, "no account given (-a)", 0);
    }

    /* Refuse the one invocation that puts the secret on a screen. The
     * backend always gives this process a pipe; a terminal means a human
     * — or an agent driving a shell — asking to read the value out. This
     * is a speed bump, not a boundary: `pdt-secret-reader ... | cat`
     * defeats it, and so does anything else that redirects stdout. */
    if (isatty(STDOUT_FILENO)) {
        fail(exit_tty,
             "refusing to write the secret to a terminal; "
             "stdout must be a pipe", 0);
    }

    keychain_path = require_keychain(keychain_path);
    SecKeychainRef keychain = open_keychain(keychain_path);

    /* Two attempts, because an item may have been filed with a service
     * and the production command line does not name one. The first asks
     * by account alone; the second only runs when a service was given
     * explicitly, and asks for that one. */
    UInt32 length = 0;
    void *data = NULL;
    SecKeychainItemRef item = NULL;
    status = SecKeychainFindGenericPassword(
        keychain, 0, NULL, (UInt32)strlen(account), account, &length, &data,
        &item);
    if (status != errSecSuccess && service != NULL) {
        status = SecKeychainFindGenericPassword(
            keychain, (UInt32)strlen(service), service,
            (UInt32)strlen(account), account, &length, &data, &item);
    }
    if (status != errSecSuccess) {
        CFRelease(keychain);
        fail(status_exit_code(status), "no item for that account", status);
    }
    if (data == NULL) {
        if (item != NULL) {
            CFRelease(item);
        }
        CFRelease(keychain);
        fail(exit_no_data, "the item yielded no data", 0);
    }

    size_t written = fwrite(data, 1, length, stdout);
    fflush(stdout);

    SecKeychainItemFreeContent(NULL, data);
    if (item != NULL) {
        CFRelease(item);
    }
    CFRelease(keychain);

    if (written != length) {
        fail(exit_no_data, "the secret could not be written in full", 0);
    }
    return exit_ok;
}
