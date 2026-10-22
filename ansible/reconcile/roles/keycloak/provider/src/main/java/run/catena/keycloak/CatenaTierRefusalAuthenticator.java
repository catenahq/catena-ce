package run.catena.keycloak;

import jakarta.ws.rs.core.Response;

import org.keycloak.authentication.AuthenticationFlowContext;
import org.keycloak.authentication.AuthenticationFlowError;
import org.keycloak.authentication.Authenticator;
import org.keycloak.events.Errors;
import org.keycloak.models.KeycloakSession;
import org.keycloak.models.RealmModel;
import org.keycloak.models.UserModel;

/**
 * Refuses the sign-in with the access-denied page, like Keycloak's Deny Access
 * step. Its factory sits in a reference category the brute-force guard does
 * not count, so a refusal on tier does not lock the account.
 */
public class CatenaTierRefusalAuthenticator implements Authenticator {

    static final String MESSAGE_KEY = "access-denied";

    @Override
    public void authenticate(AuthenticationFlowContext context) {
        context.getEvent().error(Errors.ACCESS_DENIED);
        Response challenge = context.form()
                .setError(MESSAGE_KEY)
                .createErrorPage(Response.Status.UNAUTHORIZED);
        context.failure(AuthenticationFlowError.ACCESS_DENIED, challenge);
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
