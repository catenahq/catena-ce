package run.catena.keycloak;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.Map;
import java.util.Objects;
import java.util.function.LongSupplier;

import jakarta.ws.rs.core.MultivaluedMap;
import jakarta.ws.rs.core.Response;

import org.jboss.logging.Logger;
import org.keycloak.authentication.AuthenticationFlowContext;
import org.keycloak.authentication.AuthenticationFlowError;
import org.keycloak.authentication.Authenticator;
import org.keycloak.common.util.SecretGenerator;
import org.keycloak.events.Errors;
import org.keycloak.forms.login.LoginFormsProvider;
import org.keycloak.models.ClientModel;
import org.keycloak.models.KeycloakSession;
import org.keycloak.models.RealmModel;
import org.keycloak.models.RoleModel;
import org.keycloak.models.UserModel;
import org.keycloak.models.utils.KeycloakModelUtils;
import org.keycloak.services.managers.BruteForceProtector;
import org.keycloak.sessions.AuthenticationSessionModel;

/**
 * A sign-in step that asks for an email address and signs in the client who
 * types the code mailed to it. The address post only records the address and
 * hands the work to a background job, so it never reads an account: the page,
 * the status and the time are the same for every address. The job mails the
 * code when the address belongs to an eligible client; the code post answers
 * wrong, expired, refused, locked and unknown the same way, and a code that
 * does not sign in counts toward the lockout of the account the address names.
 */
public class CatenaClientEmailCodeAuthenticator implements Authenticator {

    /** Hands a code to the background job that mails it. */
    interface Jobs {
        void submit(String realmId, String clientId, String address, String code);
    }

    /** Mails the code to a user; the session's context holds the realm and the client. */
    interface Mailer {
        boolean send(KeycloakSession session, UserModel user, String code);
    }

    static final String TIER_ADMIN_ROLE = "catena-tier-admin";
    static final String TIER_STAFF_ROLE = "catena-tier-staff";
    static final String MANAGEMENT_CLIENT = "realm-management";
    static final int MAX_SENDS = 5;
    static final int RESEND_INTERVAL_SECONDS = 30;
    static final int CODE_LIFETIME_SECONDS = 600;
    static final int CODE_LENGTH = 6;
    static final int MAX_ADDRESS_LENGTH = 255;

    static final String NOTE_ADDRESS = "catena-email-code-address";
    static final String NOTE_CODE = "catena-email-code-code";
    static final String NOTE_ISSUED_AT = "catena-email-code-issued-at";
    static final String NOTE_SENDS = "catena-email-code-sends";
    static final String NOTE_WRONG = "catena-email-code-wrong";
    static final int MAX_WRONG = 5;

    /** The note the lockout reads to find the account a failed sign-in counts against. */
    static final String ATTEMPTED_USERNAME = "ATTEMPTED_USERNAME";

    static final String PARAM_ADDRESS = "address";
    static final String PARAM_CODE = "code";
    static final String PARAM_RESEND = "resend";

    static final String TEMPLATE = "catena-email-code.ftl";
    static final String ATTR_STATE = "catenaEmailCodeState";
    static final String STATE_ADDRESS = "address";
    static final String STATE_CODE = "code";

    static final String MESSAGE_ADDRESS_INVALID = "catena-email-code-address-invalid";
    static final String MESSAGE_CODE_INVALID = "catena-email-code-invalid";
    static final String MESSAGE_TOO_SOON = "catena-email-code-too-soon";
    static final String MESSAGE_LIMIT = "catena-email-code-limit";

    private static final Logger LOG = Logger.getLogger(CatenaClientEmailCodeAuthenticator.class);

    private final Jobs jobs;
    private final LongSupplier clock;

    CatenaClientEmailCodeAuthenticator(Jobs jobs, LongSupplier clock) {
        this.jobs = jobs;
        this.clock = clock;
    }

    @Override
    public boolean requiresUser() {
        return false;
    }

    @Override
    public boolean configuredFor(KeycloakSession session, RealmModel realm, UserModel user) {
        return true;
    }

    @Override
    public void setRequiredActions(KeycloakSession session, RealmModel realm, UserModel user) {
    }

    @Override
    public void authenticate(AuthenticationFlowContext context) {
        AuthenticationSessionModel session = context.getAuthenticationSession();
        UserModel known = context.getUser();
        if (known != null && !codeIsLive(session)) {
            Response refused = gate(context);
            if (refused != null) {
                context.challenge(refused);
                return;
            }
            send(context, addressOf(known));
        }
        context.challenge(page(context, codeIsLive(session) ? STATE_CODE : STATE_ADDRESS, null));
    }

    @Override
    public void action(AuthenticationFlowContext context) {
        MultivaluedMap<String, String> posted = context.getHttpRequest().getDecodedFormParameters();
        if (posted.containsKey(PARAM_ADDRESS)) {
            addressPost(context, posted.getFirst(PARAM_ADDRESS));
        } else if (posted.containsKey(PARAM_RESEND)) {
            resendPost(context);
        } else {
            codePost(context, posted.getFirst(PARAM_CODE));
        }
    }

    private void addressPost(AuthenticationFlowContext context, String typed) {
        UserModel known = context.getUser();
        String address = known != null ? addressOf(known) : typed == null ? "" : typed.trim();
        if (address.isEmpty() || address.length() > MAX_ADDRESS_LENGTH) {
            context.forceChallenge(page(context, STATE_ADDRESS, MESSAGE_ADDRESS_INVALID));
            return;
        }
        Response refused = gate(context);
        if (refused != null) {
            context.forceChallenge(refused);
            return;
        }
        send(context, address);
        context.forceChallenge(page(context, STATE_CODE, null));
    }

    private void resendPost(AuthenticationFlowContext context) {
        String address = context.getAuthenticationSession().getAuthNote(NOTE_ADDRESS);
        if (address == null) {
            context.forceChallenge(page(context, STATE_ADDRESS, null));
            return;
        }
        Response refused = gate(context);
        if (refused != null) {
            context.forceChallenge(refused);
            return;
        }
        send(context, address);
        context.forceChallenge(page(context, STATE_CODE, null));
    }

    private void codePost(AuthenticationFlowContext context, String typed) {
        AuthenticationSessionModel session = context.getAuthenticationSession();
        String expected = session.getAuthNote(NOTE_CODE);
        String address = session.getAuthNote(NOTE_ADDRESS);
        String given = typed == null ? "" : typed.trim();
        if (expected == null || address == null || !codeIsLive(session)
                || !MessageDigest.isEqual(given.getBytes(StandardCharsets.UTF_8),
                        expected.getBytes(StandardCharsets.UTF_8))) {
            wrong(context);
            return;
        }
        UserModel user = find(context.getSession(), context.getRealm(), address);
        UserModel known = context.getUser();
        if (!eligible(context.getSession(), context.getRealm(), user, context.getProtector())
                || (known != null && !Objects.equals(known.getId(), user.getId()))) {
            wrong(context);
            return;
        }
        for (String note : new String[] {NOTE_ADDRESS, NOTE_CODE, NOTE_ISSUED_AT, NOTE_SENDS, NOTE_WRONG}) {
            session.removeAuthNote(note);
        }
        user.setEmailVerified(true);
        context.getEvent().user(user);
        context.setUser(user);
        context.success();
    }

    /**
     * The one answer to a code that does not sign in. The failure counts against the account the
     * code was sent for, whatever the password form wrote to the note since, and the fifth wrong
     * post drops the code, so a new one is needed.
     */
    private void wrong(AuthenticationFlowContext context) {
        AuthenticationSessionModel session = context.getAuthenticationSession();
        String address = session.getAuthNote(NOTE_ADDRESS);
        if (address != null) {
            session.setAuthNote(ATTEMPTED_USERNAME, address);
        }
        long wrongs = parse(session.getAuthNote(NOTE_WRONG)) + 1;
        session.setAuthNote(NOTE_WRONG, Long.toString(wrongs));
        if (wrongs >= MAX_WRONG) {
            session.removeAuthNote(NOTE_CODE);
        }
        context.getEvent().error(Errors.INVALID_USER_CREDENTIALS);
        context.failureChallenge(AuthenticationFlowError.INVALID_CREDENTIALS,
                page(context, STATE_CODE, MESSAGE_CODE_INVALID));
    }

    /** The code page with the wait or limit message when a new code is not allowed yet; null when it is. */
    private Response gate(AuthenticationFlowContext context) {
        AuthenticationSessionModel session = context.getAuthenticationSession();
        if (parse(session.getAuthNote(NOTE_SENDS)) >= MAX_SENDS) {
            return page(context, STATE_CODE, MESSAGE_LIMIT);
        }
        long issuedAt = parse(session.getAuthNote(NOTE_ISSUED_AT));
        long waitMillis = issuedAt + RESEND_INTERVAL_SECONDS * 1000L - clock.getAsLong();
        if (issuedAt != 0 && waitMillis > 0) {
            return page(context, STATE_CODE, MESSAGE_TOO_SOON, (waitMillis + 999) / 1000);
        }
        return null;
    }

    private void send(AuthenticationFlowContext context, String address) {
        AuthenticationSessionModel session = context.getAuthenticationSession();
        String code = SecretGenerator.getInstance().randomString(CODE_LENGTH, SecretGenerator.DIGITS);
        session.setAuthNote(NOTE_ADDRESS, address);
        session.setAuthNote(NOTE_CODE, code);
        session.setAuthNote(NOTE_ISSUED_AT, Long.toString(clock.getAsLong()));
        session.setAuthNote(NOTE_SENDS, Long.toString(parse(session.getAuthNote(NOTE_SENDS)) + 1));
        session.setAuthNote(NOTE_WRONG, "0");
        session.setAuthNote(ATTEMPTED_USERNAME, address);
        jobs.submit(context.getRealm().getId(), session.getClient().getId(), address, code);
    }

    private boolean codeIsLive(AuthenticationSessionModel session) {
        long issuedAt = parse(session.getAuthNote(NOTE_ISSUED_AT));
        return session.getAuthNote(NOTE_CODE) != null
                && issuedAt != 0
                && clock.getAsLong() - issuedAt <= CODE_LIFETIME_SECONDS * 1000L;
    }

    private Response page(AuthenticationFlowContext context, String state, String error, Object... params) {
        LoginFormsProvider form = context.form()
                .setExecution(context.getExecution().getId())
                .setAttribute(ATTR_STATE, state);
        if (error != null) {
            form = form.setError(error, params);
        }
        return form.createForm(TEMPLATE);
    }

    @Override
    public void close() {
    }

    /** The address a known user's code goes to: their email, else their username. */
    private static String addressOf(UserModel user) {
        return user.getEmail() == null || user.getEmail().isBlank() ? user.getUsername() : user.getEmail();
    }

    private static long parse(String note) {
        try {
            return note == null ? 0 : Long.parseLong(note);
        } catch (NumberFormatException e) {
            return 0;
        }
    }

    private static UserModel find(KeycloakSession session, RealmModel realm, String address) {
        try {
            return KeycloakModelUtils.findUserByNameOrEmail(session, realm, address);
        } catch (RuntimeException e) {
            return null;
        }
    }

    /**
     * Whether the account may sign in with a code: enabled, a person's own account (no service
     * account), not locked out, with an email, outside the staff and administrator tiers, holding no
     * realm-management role, in a realm that holds both tier roles and an outgoing mail host.
     */
    static boolean eligible(KeycloakSession session, RealmModel realm, UserModel user, BruteForceProtector protector) {
        if (user == null || !hasMailSettings(realm) || !user.isEnabled()
                || user.getServiceAccountClientLink() != null
                || user.getEmail() == null || user.getEmail().isBlank()) {
            return false;
        }
        if (realm.isBruteForceProtected() && (protector.isPermanentlyLockedOut(session, realm, user)
                || protector.isTemporarilyDisabled(session, realm, user))) {
            return false;
        }
        RoleModel staff = realm.getRole(TIER_STAFF_ROLE);
        RoleModel admin = realm.getRole(TIER_ADMIN_ROLE);
        if (staff == null || admin == null || user.hasRole(staff) || user.hasRole(admin)) {
            return false;
        }
        ClientModel management = realm.getClientByClientId(MANAGEMENT_CLIENT);
        return management == null || management.getRolesStream().noneMatch(user::hasRole);
    }

    static boolean hasMailSettings(RealmModel realm) {
        Map<String, String> smtp = realm.getSmtpConfig();
        if (smtp == null) {
            return false;
        }
        String host = smtp.get("host");
        return host != null && !host.isBlank();
    }

    /**
     * The background job: mails the code when the address finds an eligible account. The session's
     * context holds the realm and client the mail template reads. A failure is logged, never shown.
     */
    static void deliver(KeycloakSession session, String realmId, String clientId, String address, String code,
            Mailer mailer) {
        RealmModel realm = session.realms().getRealm(realmId);
        if (realm == null) {
            return;
        }
        ClientModel client = realm.getClientById(clientId);
        session.getContext().setRealm(realm);
        session.getContext().setClient(client);
        UserModel user = find(session, realm, address);
        if (eligible(session, realm, user, session.getProvider(BruteForceProtector.class))) {
            if (!mailer.send(session, user, code)) {
                LOG.warn("The sign-in code could not be mailed");
            }
        }
    }
}
