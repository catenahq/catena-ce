package run.catena.keycloak;

import static org.junit.jupiter.api.Assertions.assertArrayEquals;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.RETURNS_SELF;
import static org.mockito.Mockito.doAnswer;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.mockingDetails;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.verifyNoInteractions;
import static org.mockito.Mockito.when;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.stream.Stream;

import jakarta.ws.rs.core.MultivaluedHashMap;
import jakarta.ws.rs.core.MultivaluedMap;
import jakarta.ws.rs.core.Response;

import org.junit.jupiter.api.Test;
import org.keycloak.authentication.AuthenticationFlowContext;
import org.keycloak.authentication.AuthenticationFlowError;
import org.keycloak.events.Errors;
import org.keycloak.events.EventBuilder;
import org.keycloak.forms.login.LoginFormsProvider;
import org.keycloak.http.HttpRequest;
import org.keycloak.models.AuthenticationExecutionModel;
import org.keycloak.models.AuthenticationExecutionModel.Requirement;
import org.keycloak.models.ClientModel;
import org.keycloak.models.KeycloakContext;
import org.keycloak.models.KeycloakSession;
import org.keycloak.models.RealmModel;
import org.keycloak.models.RealmProvider;
import org.keycloak.models.RoleModel;
import org.keycloak.models.UserModel;
import org.keycloak.models.UserProvider;
import org.keycloak.services.managers.BruteForceProtector;
import org.keycloak.sessions.AuthenticationSessionModel;

class CatenaClientEmailCodeTest {

    private static final long T0 = 1_000_000L;
    private static final String ADDRESS = "person@example.test";

    /** The kinds of account an address can name; only CLIENT may sign in with a code. */
    private enum Kind {
        CLIENT, UNKNOWN, DISABLED, LOCKED, LOCKED_PERMANENT, STAFF, ADMIN, STAFF_AND_CLIENT, REALM_MANAGEMENT,
        TIER_ROLE_MISSING, MAIL_OFF, SERVICE_ACCOUNT, NO_EMAIL
    }

    private static final class Jobs implements CatenaClientEmailCodeAuthenticator.Jobs {
        final List<String[]> submitted = new ArrayList<>();

        @Override
        public void submit(String realmId, String clientId, String address, String code) {
            submitted.add(new String[] {realmId, clientId, address, code});
        }
    }

    private final CatenaClientEmailCodeAuthenticatorFactory factory = new CatenaClientEmailCodeAuthenticatorFactory();
    private final Jobs jobs = new Jobs();
    private long clock = T0;
    private final CatenaClientEmailCodeAuthenticator step =
            new CatenaClientEmailCodeAuthenticator(jobs, () -> clock);

    private static final class Fixture {
        final Kind kind;
        final AuthenticationFlowContext context = mock(AuthenticationFlowContext.class);
        final KeycloakSession session = mock(KeycloakSession.class);
        final KeycloakContext keycloakContext = mock(KeycloakContext.class);
        final RealmProvider realms = mock(RealmProvider.class);
        final UserProvider users = mock(UserProvider.class);
        final RealmModel realm = mock(RealmModel.class);
        final ClientModel client = mock(ClientModel.class);
        final ClientModel management = mock(ClientModel.class);
        final UserModel user = mock(UserModel.class);
        final EventBuilder event = mock(EventBuilder.class);
        final LoginFormsProvider form = mock(LoginFormsProvider.class, RETURNS_SELF);
        final Response page = mock(Response.class);
        final BruteForceProtector protector = mock(BruteForceProtector.class);
        final Map<String, String> notes = new HashMap<>();
        final MultivaluedMap<String, String> posted = new MultivaluedHashMap<>();

        Fixture(Kind kind) {
            this.kind = kind;
            RoleModel staff = mock(RoleModel.class);
            RoleModel admin = mock(RoleModel.class);
            RoleModel managementRole = mock(RoleModel.class);
            if (kind != Kind.TIER_ROLE_MISSING) {
                when(realm.getRole("catena-tier-staff")).thenReturn(staff);
                when(realm.getRole("catena-tier-admin")).thenReturn(admin);
            }
            when(user.hasRole(staff)).thenReturn(kind == Kind.STAFF || kind == Kind.STAFF_AND_CLIENT);
            when(user.hasRole(admin)).thenReturn(kind == Kind.ADMIN);
            when(user.hasRole(managementRole)).thenReturn(kind == Kind.REALM_MANAGEMENT);
            when(management.getRolesStream()).thenAnswer(call -> Stream.of(managementRole));
            when(realm.getClientByClientId("realm-management")).thenReturn(management);
            when(user.isEnabled()).thenReturn(kind != Kind.DISABLED);
            when(user.getEmail()).thenReturn(kind == Kind.NO_EMAIL ? null : ADDRESS);
            when(user.getServiceAccountClientLink()).thenReturn(kind == Kind.SERVICE_ACCOUNT ? "sa" : null);
            Map<String, String> smtp = new HashMap<>();
            if (kind != Kind.MAIL_OFF) {
                smtp.put("host", "smtp.example.test");
            }
            when(realm.getSmtpConfig()).thenReturn(smtp);
            when(realm.getId()).thenReturn("realm-id");
            when(realm.isLoginWithEmailAllowed()).thenReturn(true);
            when(realm.isBruteForceProtected()).thenReturn(kind == Kind.LOCKED || kind == Kind.LOCKED_PERMANENT);
            when(protector.isTemporarilyDisabled(session, realm, user)).thenReturn(kind == Kind.LOCKED);
            when(protector.isPermanentlyLockedOut(session, realm, user)).thenReturn(kind == Kind.LOCKED_PERMANENT);
            when(user.getId()).thenReturn("user-id");
            when(client.getId()).thenReturn("client-internal");
            when(realm.getClientById("client-internal")).thenReturn(client);

            UserModel found = kind == Kind.UNKNOWN ? null : user;
            when(users.getUserByEmail(realm, ADDRESS)).thenReturn(found);
            when(users.getUserByUsername(realm, ADDRESS)).thenReturn(found);
            when(session.users()).thenReturn(users);
            when(session.realms()).thenReturn(realms);
            when(realms.getRealm("realm-id")).thenReturn(realm);
            when(session.getContext()).thenReturn(keycloakContext);
            when(session.getProvider(BruteForceProtector.class)).thenReturn(protector);

            AuthenticationSessionModel auth = mock(AuthenticationSessionModel.class);
            when(auth.getAuthNote(anyString())).thenAnswer(call -> notes.get(call.<String>getArgument(0)));
            doAnswer(call -> notes.put(call.getArgument(0), call.getArgument(1)))
                    .when(auth).setAuthNote(anyString(), anyString());
            doAnswer(call -> notes.remove(call.<String>getArgument(0))).when(auth).removeAuthNote(anyString());
            when(auth.getClient()).thenReturn(client);

            HttpRequest request = mock(HttpRequest.class);
            when(request.getDecodedFormParameters()).thenReturn(posted);
            AuthenticationExecutionModel execution = mock(AuthenticationExecutionModel.class);
            when(execution.getId()).thenReturn("exec-1");

            when(context.getRealm()).thenReturn(realm);
            when(context.getSession()).thenReturn(session);
            when(context.getEvent()).thenReturn(event);
            when(context.getAuthenticationSession()).thenReturn(auth);
            when(context.getHttpRequest()).thenReturn(request);
            when(context.getExecution()).thenReturn(execution);
            when(context.getProtector()).thenReturn(protector);
            when(context.form()).thenReturn(form);
            when(form.createForm(anyString())).thenReturn(page);
        }

        void post(String name, String value) {
            posted.clear();
            posted.add(name, value);
        }
    }

    private static Stream<Kind> kinds() {
        return Arrays.stream(Kind.values());
    }

    /** The outcome calls on the flow context, with pages made comparable across fixtures. */
    private static List<String> outcome(Fixture f) {
        Set<String> names = Set.of("challenge", "forceChallenge", "failureChallenge", "failure", "success",
                "setUser", "clearUser");
        return mockingDetails(f.context).getInvocations().stream()
                .filter(i -> names.contains(i.getMethod().getName()))
                .map(i -> i.getMethod().getName() + Arrays.stream(i.getArguments())
                        .map(a -> a instanceof Response ? "<page>" : String.valueOf(a)).toList())
                .toList();
    }

    /** The calls made on the form builder, which carry the template, attributes, messages and parameters. */
    private static List<String> built(Fixture f) {
        return mockingDetails(f.form).getInvocations().stream()
                .map(i -> i.getMethod().getName() + Arrays.deepToString(i.getArguments()))
                .toList();
    }

    private String sentCode() {
        return jobs.submitted.get(jobs.submitted.size() - 1)[3];
    }

    private void address(Fixture f) {
        f.post("address", ADDRESS);
        step.action(f.context);
    }

    // --- the factory --------------------------------------------------------------

    @Test
    void theFactoryIsFixedAndCountsAWrongCodeTowardTheLockout() {
        assertEquals("catena-client-email-code", factory.getId());
        assertEquals("otp", factory.getReferenceCategory());
        assertFalse(factory.isConfigurable());
        assertTrue(factory.getConfigProperties().isEmpty());
        assertFalse(factory.isUserSetupAllowed());
        assertArrayEquals(new Requirement[] {Requirement.ALTERNATIVE, Requirement.DISABLED},
                factory.getRequirementChoices());
        assertFalse(factory.create(null).requiresUser());
    }

    // --- the address page ---------------------------------------------------------

    @Test
    void theFirstPageAsksForTheAddressWhateverTheRealmHolds() {
        List<String> shown = null;
        for (Kind kind : Kind.values()) {
            Fixture f = new Fixture(kind);
            step.authenticate(f.context);
            verify(f.context).challenge(f.page);
            verify(f.context, never()).setUser(any());
            verifyNoInteractions(f.users);
            assertTrue(jobs.submitted.isEmpty(), kind.name());
            verify(f.form).setAttribute("catenaEmailCodeState", "address");
            verify(f.form).createForm("catena-email-code.ftl");
            List<String> calls = built(f);
            shown = shown == null ? calls : shown;
            assertEquals(shown, calls, kind.name());
        }
    }

    @Test
    void theAddressPostAnswersTheSameForEveryKindAndReadsNoAccount() {
        List<String> shownOutcome = null;
        List<String> shownForm = null;
        for (Kind kind : Kind.values()) {
            Fixture f = new Fixture(kind);
            jobs.submitted.clear();
            address(f);
            assertEquals(1, jobs.submitted.size(), kind.name());
            String[] job = jobs.submitted.get(0);
            assertArrayEquals(new String[] {"realm-id", "client-internal", ADDRESS, job[3]}, job);
            assertTrue(job[3].matches("[0-9]{6}"), job[3]);
            assertEquals(ADDRESS, f.notes.get("ATTEMPTED_USERNAME"));
            verify(f.context).forceChallenge(f.page);
            verify(f.context, never()).setUser(any());
            verifyNoInteractions(f.users);
            verify(f.realm, never()).getRole(anyString());
            verify(f.realm, never()).getClientByClientId(anyString());
            verifyNoInteractions(f.user);
            List<String> o = outcome(f);
            List<String> b = built(f);
            shownOutcome = shownOutcome == null ? o : shownOutcome;
            shownForm = shownForm == null ? b : shownForm;
            assertEquals(shownOutcome, o, kind.name());
            assertEquals(shownForm, b, kind.name());
        }
        assertTrue(shownForm.contains("setAttribute[catenaEmailCodeState, code]"));
    }

    @Test
    void anEmptyOrTooLongAddressGetsTheAddressPageAgainAndMailsNothing() {
        Fixture f = new Fixture(Kind.CLIENT);
        f.post("address", "   ");
        step.action(f.context);
        f.post("address", "a".repeat(256));
        step.action(f.context);
        assertTrue(jobs.submitted.isEmpty());
        verify(f.form, org.mockito.Mockito.times(2)).setError("catena-email-code-address-invalid");
        verify(f.form, org.mockito.Mockito.times(2)).setAttribute("catenaEmailCodeState", "address");
    }

    @Test
    void anAddressIsTrimmed() {
        Fixture f = new Fixture(Kind.CLIENT);
        f.post("address", "  " + ADDRESS + " ");
        step.action(f.context);
        assertEquals(ADDRESS, jobs.submitted.get(0)[2]);
    }

    @Test
    void aRefreshShowsTheCodePageWithoutMailingAgain() {
        Fixture f = new Fixture(Kind.CLIENT);
        address(f);
        step.authenticate(f.context);
        assertEquals(1, jobs.submitted.size());
        verify(f.form, org.mockito.Mockito.times(2)).setAttribute("catenaEmailCodeState", "code");
    }

    // --- the job ------------------------------------------------------------------

    @Test
    void theJobMailsTheCodeToAnEligibleClientOnly() {
        for (Kind kind : Kind.values()) {
            Fixture f = new Fixture(kind);
            List<Object[]> mailed = new ArrayList<>();
            CatenaClientEmailCodeAuthenticator.deliver(f.session, "realm-id", "client-internal", ADDRESS, "123456",
                    (s, user, code) -> {
                        mailed.add(new Object[] {user, code});
                        return true;
                    });
            if (kind == Kind.CLIENT) {
                assertEquals(1, mailed.size());
                assertEquals(f.user, mailed.get(0)[0]);
                assertEquals("123456", mailed.get(0)[1]);
                verify(f.keycloakContext).setRealm(f.realm);
                verify(f.keycloakContext).setClient(f.client);
            } else {
                assertTrue(mailed.isEmpty(), kind.name());
            }
        }
    }

    @Test
    void theJobMailsNothingForAnUnknownRealm() {
        Fixture f = new Fixture(Kind.CLIENT);
        CatenaClientEmailCodeAuthenticator.deliver(f.session, "other", "client-internal", ADDRESS, "123456",
                (s, user, code) -> {
                    throw new AssertionError("mailed");
                });
    }

    // --- the code -----------------------------------------------------------------

    private void assertRefusedLikeAWrongCode(Fixture f) {
        verify(f.context).failureChallenge(AuthenticationFlowError.INVALID_CREDENTIALS, f.page);
        verify(f.context, never()).setUser(any());
        verify(f.context, never()).success();
        verify(f.event).error(Errors.INVALID_USER_CREDENTIALS);
        verify(f.form).setError("catena-email-code-invalid");
    }

    @Test
    void theRightCodeSignsInAClientAndMarksTheAddressVerified() {
        Fixture f = new Fixture(Kind.CLIENT);
        address(f);
        f.post("code", sentCode());
        step.action(f.context);
        verify(f.context).setUser(f.user);
        verify(f.user).setEmailVerified(true);
        verify(f.context).success();
        verify(f.context, never()).failureChallenge(any(), any());
        assertFalse(f.notes.containsKey("catena-email-code-code"));
    }

    @Test
    void aWrongCodeIsTheSameRefusalForEveryKind() {
        List<String> shown = null;
        for (Kind kind : Kind.values()) {
            Fixture f = new Fixture(kind);
            address(f);
            f.post("code", sentCode().equals("000000") ? "111111" : "000000");
            step.action(f.context);
            assertRefusedLikeAWrongCode(f);
            verifyNoInteractions(f.users);
            List<String> o = outcome(f).subList(1, outcome(f).size());
            shown = shown == null ? o : shown;
            assertEquals(shown, o, kind.name());
        }
    }

    @Test
    void theRightCodeIsRefusedLikeAWrongOneForEveryKindThatMayNotSignInWithIt() {
        List<String> shown = null;
        for (Kind kind : Kind.values()) {
            if (kind == Kind.CLIENT) {
                continue;
            }
            Fixture f = new Fixture(kind);
            address(f);
            f.post("code", sentCode());
            step.action(f.context);
            assertRefusedLikeAWrongCode(f);
            verify(f.user, never()).setEmailVerified(true);
            List<String> o = outcome(f).subList(1, outcome(f).size());
            shown = shown == null ? o : shown;
            assertEquals(shown, o, kind.name());
        }
    }

    @Test
    void aCodeOlderThanTenMinutesIsRefusedLikeAWrongOne() {
        Fixture late = new Fixture(Kind.CLIENT);
        address(late);
        late.post("code", sentCode());
        Fixture inTime = new Fixture(Kind.CLIENT);
        address(inTime);
        inTime.post("code", sentCode());

        clock = T0 + 600_000;
        step.action(inTime.context);
        verify(inTime.context).success();

        clock = T0 + 601_000;
        step.action(late.context);
        assertRefusedLikeAWrongCode(late);
    }

    @Test
    void aCodePostWithoutACodeIsRefused() {
        Fixture f = new Fixture(Kind.CLIENT);
        f.post("code", "123456");
        step.action(f.context);
        assertRefusedLikeAWrongCode(f);
    }

    @Test
    void aCodeIsUsableOnce() {
        Fixture f = new Fixture(Kind.CLIENT);
        address(f);
        String code = sentCode();
        f.post("code", code);
        step.action(f.context);
        verify(f.context).success();
        step.action(f.context);
        verify(f.context).failureChallenge(AuthenticationFlowError.INVALID_CREDENTIALS, f.page);
    }

    @Test
    void everyFailureCountsAgainstTheAddressTheCodeWentTo() {
        Fixture f = new Fixture(Kind.CLIENT);
        List<String> counted = new ArrayList<>();
        doAnswer(call -> counted.add(f.notes.get("ATTEMPTED_USERNAME")))
                .when(f.context).failureChallenge(any(), any());
        address(f);
        for (int post = 0; post < 3; post++) {
            f.notes.put("ATTEMPTED_USERNAME", "made-up-by-the-password-form");
            f.post("code", "not-the-code");
            step.action(f.context);
        }
        assertEquals(List.of(ADDRESS, ADDRESS, ADDRESS), counted);
    }

    @Test
    void theSixthWrongPostFailsEvenWithTheRightCode() {
        Fixture f = new Fixture(Kind.CLIENT);
        address(f);
        String code = sentCode();
        for (int post = 0; post < 5; post++) {
            f.post("code", "not-the-code");
            step.action(f.context);
        }
        f.post("code", code);
        step.action(f.context);
        verify(f.context, never()).success();
        verify(f.context, org.mockito.Mockito.times(6)).failureChallenge(AuthenticationFlowError.INVALID_CREDENTIALS, f.page);

        clock = T0 + 31_000;
        f.post("resend", "");
        step.action(f.context);
        f.post("code", sentCode());
        step.action(f.context);
        verify(f.context).success();
    }

    @Test
    void aKnownUserGetsTheirOwnCodeWhateverAddressIsTyped() {
        Fixture f = new Fixture(Kind.CLIENT);
        when(f.context.getUser()).thenReturn(f.user);
        f.post("address", "someone-else@example.test");
        step.action(f.context);
        assertEquals(1, jobs.submitted.size());
        assertEquals(ADDRESS, jobs.submitted.get(0)[2]);
    }

    @Test
    void aCodeIssuedForAnotherAddressNeverSignsInTheUserTheFlowHolds() {
        Fixture f = new Fixture(Kind.CLIENT);
        address(f);
        UserModel other = mock(UserModel.class);
        when(other.getId()).thenReturn("other-id");
        when(f.context.getUser()).thenReturn(other);
        f.post("code", sentCode());
        step.action(f.context);
        assertRefusedLikeAWrongCode(f);
        verify(f.context, never()).setUser(any());
    }

    // --- sending again ------------------------------------------------------------

    @Test
    void aNewCodeWaitsThirtySecondsAndFiveAreSentPerSignIn() {
        Fixture f = new Fixture(Kind.CLIENT);
        address(f);
        f.post("resend", "");

        clock = T0 + 29_000;
        step.action(f.context);
        verify(f.form).setError("catena-email-code-too-soon", 1L);
        assertEquals(1, jobs.submitted.size());

        clock = T0 + 30_000;
        step.action(f.context);
        assertEquals(2, jobs.submitted.size());
        for (int sent = 3; sent <= 5; sent++) {
            clock += 31_000;
            step.action(f.context);
            assertEquals(sent, jobs.submitted.size());
        }

        clock += 3_600_000;
        step.action(f.context);
        assertEquals(5, jobs.submitted.size());
        verify(f.form).setError("catena-email-code-limit");
        verify(f.context, never()).failureChallenge(any(), any());
    }

    @Test
    void anotherAddressPostDoesNotGetAroundTheLimits() {
        Fixture f = new Fixture(Kind.CLIENT);
        address(f);
        clock = T0 + 5_000;
        address(f);
        assertEquals(1, jobs.submitted.size());
        verify(f.form).setError("catena-email-code-too-soon", 25L);
    }

    @Test
    void theLimitsReadTheSignInOnlyAndAreEqualForEveryKind() {
        List<String> shown = null;
        for (Kind kind : Kind.values()) {
            Fixture f = new Fixture(kind);
            address(f);
            f.post("resend", "");
            clock = T0 + 29_000;
            step.action(f.context);
            verifyNoInteractions(f.users);
            List<String> b = built(f);
            shown = shown == null ? b : shown;
            assertEquals(shown, b, kind.name());
            clock = T0;
        }
    }

    @Test
    void aResendWithoutAnAddressAsksForTheAddress() {
        Fixture f = new Fixture(Kind.CLIENT);
        f.post("resend", "");
        step.action(f.context);
        assertTrue(jobs.submitted.isEmpty());
        verify(f.form).setAttribute("catenaEmailCodeState", "address");
    }

    // --- signing in again ---------------------------------------------------------

    @Test
    void aReAuthenticationSkipsTheAddressAndMailsTheKnownAccount() {
        Fixture f = new Fixture(Kind.CLIENT);
        when(f.context.getUser()).thenReturn(f.user);
        step.authenticate(f.context);
        assertEquals(1, jobs.submitted.size());
        assertEquals(ADDRESS, jobs.submitted.get(0)[2]);
        verify(f.form).setAttribute("catenaEmailCodeState", "code");
        verify(f.context).challenge(f.page);

        f.post("code", sentCode());
        step.action(f.context);
        verify(f.context).success();
    }
}
