package run.catena.keycloak;

import java.util.Arrays;
import java.util.Set;
import java.util.stream.Collectors;

import jakarta.ws.rs.core.Response;

import org.keycloak.authentication.AuthenticationFlowContext;
import org.keycloak.authentication.AuthenticationFlowError;
import org.keycloak.authentication.Authenticator;
import org.keycloak.events.Errors;
import org.keycloak.models.ClientModel;
import org.keycloak.models.KeycloakSession;
import org.keycloak.models.RealmModel;
import org.keycloak.models.UserModel;

/**
 * Refuses the sign-in with the access-denied page, like Keycloak's Deny Access
 * step, unless the user is a direct member of a top-level group named in the
 * comma list in the signing-in client's attribute catena.access.groups, which
 * admits the sign-in. Its factory sits in a reference category the
 * brute-force guard does not count, so a refusal on tier does not lock the
 * account.
 */
public class CatenaTierRefusalAuthenticator implements Authenticator {

    static final String MESSAGE_KEY = "access-denied";
    static final String ACCESS_GROUPS_ATTRIBUTE = "catena.access.groups";

    @Override
    public void authenticate(AuthenticationFlowContext context) {
        if (admittedByAccessGroup(context.getUser(), context.getAuthenticationSession().getClient())) {
            context.success();
            return;
        }
        context.getEvent().error(Errors.ACCESS_DENIED);
        Response challenge = context.form()
                .setError(MESSAGE_KEY)
                .createErrorPage(Response.Status.UNAUTHORIZED);
        context.failure(AuthenticationFlowError.ACCESS_DENIED, challenge);
    }

    static boolean admittedByAccessGroup(UserModel user, ClientModel client) {
        if (user == null || client == null) {
            return false;
        }
        String listed = client.getAttribute(ACCESS_GROUPS_ATTRIBUTE);
        if (listed == null) {
            return false;
        }
        Set<String> names = Arrays.stream(listed.split(","))
                .map(String::trim)
                .filter(name -> !name.isEmpty())
                .collect(Collectors.toSet());
        return user.getGroupsStream()
                .anyMatch(group -> group.getParentId() == null && names.contains(group.getName()));
    }

    @Override
    public void action(AuthenticationFlowContext context) {
    }

    @Override
    public boolean requiresUser() {
        return false;
    }

    @Override
    public boolean configuredFor(KeycloakSession session, RealmModel realm, UserModel user) {
        return false;
    }

    @Override
    public void setRequiredActions(KeycloakSession session, RealmModel realm, UserModel user) {
    }

    @Override
    public void close() {
    }
}
